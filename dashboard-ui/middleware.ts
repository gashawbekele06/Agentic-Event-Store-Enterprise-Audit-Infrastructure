import { NextRequest, NextResponse } from "next/server";

export function middleware(req: NextRequest) {
  // Suppress noise from browser extension trackers hitting hybridaction routes
  if (req.nextUrl.pathname.startsWith("/hybridaction/")) {
    return new NextResponse(null, { status: 204 });
  }
  return NextResponse.next();
}

export const config = {
  matcher: "/hybridaction/:path*",
};
