import { NextRequest, NextResponse } from "next/server";

const SESSION_COOKIE_PATTERNS = ["AWSALB", "AWSELB", "AWSELBAuthSessionCookie"];

export async function GET(req: NextRequest) {
  const domain =
    process.env.COGNITO_DOMAIN ||
    "https://your-cognito-domain.auth.region.amazoncognito.com";
  const clientId = process.env.COGNITO_CLIENT_ID || "YOUR_CLIENT_ID";
  const logoutUri =
    process.env.COGNITO_LOGOUT_URI || "https://your-app-domain.example.com";

  const logoutUrl = `${domain}/logout?client_id=${clientId}&logout_uri=${encodeURIComponent(logoutUri)}`;
  const response = NextResponse.redirect(logoutUrl);

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
