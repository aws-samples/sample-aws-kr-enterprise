import { NextRequest, NextResponse } from "next/server";

const SESSION_COOKIE_PATTERNS = ["AWSALB", "AWSELB", "AWSELBAuthSessionCookie"];

export async function GET(req: NextRequest) {
  const domain = process.env.COGNITO_DOMAIN;
  const clientId = process.env.COGNITO_CLIENT_ID;
  const logoutUri = process.env.COGNITO_LOGOUT_URI;

  // If the hosted-UI logout is not configured, clear cookies and redirect
  // home instead of sending the user to a bogus placeholder domain.
  const target =
    domain && clientId && logoutUri
      ? `${domain}/logout?client_id=${clientId}&logout_uri=${encodeURIComponent(logoutUri)}`
      : new URL("/", req.url).toString();
  const response = NextResponse.redirect(target);

  req.cookies.getAll().forEach((cookie) => {
    if (SESSION_COOKIE_PATTERNS.some((p) => cookie.name.startsWith(p))) {
      response.cookies.set(cookie.name, "", {
        maxAge: 0,
        path: "/",
        secure: true,
        httpOnly: true,
      });
    }
  });

  return response;
}
