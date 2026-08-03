"""Authentication routes — signup, login, verify email, refresh token."""

import os
import logging

import boto3
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, EmailStr, field_validator

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/auth", tags=["auth"])

REGION = os.environ.get("AWS_REGION", "us-west-2")
CLIENT_ID = os.environ.get("COGNITO_CLIENT_ID", "")
cognito = boto3.client("cognito-idp", region_name=REGION)


class SignupRequest(BaseModel):
    email: EmailStr
    password: str

    @field_validator("password")
    @classmethod
    def validate_password(cls, v: str) -> str:
        if len(v) < 8:
            raise ValueError("Password must be at least 8 characters")
        if not any(c.isupper() for c in v):
            raise ValueError("Password must contain at least one uppercase letter")
        if not any(c.islower() for c in v):
            raise ValueError("Password must contain at least one lowercase letter")
        if not any(c.isdigit() for c in v):
            raise ValueError("Password must contain at least one number")
        return v


class VerifyRequest(BaseModel):
    email: EmailStr
    code: str


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class RefreshRequest(BaseModel):
    refresh_token: str


@router.post("/signup")
async def signup(req: SignupRequest):
    """Register a new user. Sends verification code to email."""
    try:
        cognito.sign_up(
            ClientId=CLIENT_ID,
            Username=req.email,
            Password=req.password,
            UserAttributes=[
                {"Name": "email", "Value": req.email},
            ],
        )
        return {
            "message": "Signup successful. Please check your email for verification code.",
            "email": req.email,
        }
    except cognito.exceptions.UsernameExistsException:
        raise HTTPException(status_code=409, detail="User already exists")
    except cognito.exceptions.InvalidPasswordException as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Signup error: {e}")
        raise HTTPException(status_code=500, detail="Signup failed")


@router.post("/verify")
async def verify_email(req: VerifyRequest):
    """Verify email with the code sent during signup."""
    try:
        cognito.confirm_sign_up(
            ClientId=CLIENT_ID,
            Username=req.email,
            ConfirmationCode=req.code,
        )
        return {"message": "Email verified successfully. You can now login."}
    except cognito.exceptions.CodeMismatchException:
        raise HTTPException(status_code=400, detail="Invalid verification code")
    except cognito.exceptions.ExpiredCodeException:
        raise HTTPException(status_code=400, detail="Verification code expired")
    except Exception as e:
        logger.error(f"Verify error: {e}")
        raise HTTPException(status_code=500, detail="Verification failed")


class ResendRequest(BaseModel):
    email: EmailStr


@router.post("/resend-code")
async def resend_code(req: ResendRequest):
    """Resend email verification code."""
    try:
        cognito.resend_confirmation_code(ClientId=CLIENT_ID, Username=req.email)
        return {"message": "Verification code resent"}
    except Exception as e:
        logger.error(f"Resend error: {e}")
        raise HTTPException(status_code=500, detail="Failed to resend code")


@router.post("/login")
async def login(req: LoginRequest):
    """Authenticate and return JWT tokens."""
    try:
        result = cognito.initiate_auth(
            ClientId=CLIENT_ID,
            AuthFlow="USER_PASSWORD_AUTH",
            AuthParameters={
                "USERNAME": req.email,
                "PASSWORD": req.password,
            },
        )
        auth_result = result.get("AuthenticationResult", {})
        return {
            "access_token": auth_result.get("AccessToken"),
            "id_token": auth_result.get("IdToken"),
            "refresh_token": auth_result.get("RefreshToken"),
            "expires_in": auth_result.get("ExpiresIn"),
            "token_type": "Bearer",
        }
    except cognito.exceptions.NotAuthorizedException:
        raise HTTPException(status_code=401, detail="Invalid email or password")
    except cognito.exceptions.UserNotConfirmedException:
        raise HTTPException(
            status_code=403,
            detail="Email not verified. Please check your email for verification code.",
        )
    except Exception as e:
        logger.error(f"Login error: {e}")
        raise HTTPException(status_code=500, detail="Login failed")


@router.post("/refresh")
async def refresh_token(req: RefreshRequest):
    """Refresh access token using refresh token."""
    try:
        result = cognito.initiate_auth(
            ClientId=CLIENT_ID,
            AuthFlow="REFRESH_TOKEN_AUTH",
            AuthParameters={
                "REFRESH_TOKEN": req.refresh_token,
            },
        )
        auth_result = result.get("AuthenticationResult", {})
        return {
            "access_token": auth_result.get("AccessToken"),
            "id_token": auth_result.get("IdToken"),
            "expires_in": auth_result.get("ExpiresIn"),
            "token_type": "Bearer",
        }
    except cognito.exceptions.NotAuthorizedException:
        raise HTTPException(status_code=401, detail="Invalid refresh token")
    except Exception as e:
        logger.error(f"Refresh error: {e}")
        raise HTTPException(status_code=500, detail="Token refresh failed")


@router.get("/me")
async def get_current_user(request: Request):
    """Get current authenticated user info.

    Authenticated route: the Cognito middleware verifies the Bearer token and
    populates request.state.user before this handler runs.
    """
    user = getattr(request.state, "user", None)
    if user:
        return {
            "email": user.get("email", ""),
            "sub": user.get("sub", ""),
        }
    raise HTTPException(status_code=401, detail="Not authenticated")
