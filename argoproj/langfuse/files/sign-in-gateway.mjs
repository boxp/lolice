// BOXP-428: Langfuse's sign-in page has no setting that starts a provider on its own. Everyone who
// reaches this hostname already passed Cloudflare Access, so this answers /auth/sign-in by starting
// the Access OIDC login. Langfuse still checks the ID token; this holds no credential and trusts no header.
//
// A signed-out visit to any other page does not load /auth/sign-in from the server: the app layout
// calls router.replace("/auth/sign-in?targetPath=..."), and the Next.js router then fetches the page
// props from /_next/data/<buildId>/<locale>/auth/sign-in.json (Langfuse turns on i18n with "en").
// That request is answered here too, with the redirect form getServerSideProps itself uses
// (pageProps.__N_REDIRECT), so the router leaves for the issuer instead of rendering the button page.
import { createServer } from "node:http";

const web = process.env.LANGFUSE_WEB_URL;
const base = process.env.NEXTAUTH_URL.replace(/\/+$/, "");
const issuer = process.env.AUTH_CUSTOM_ISSUER.replace(/\/+$/, "");

const callbackUrl = (query) => {
  const target = query.get("targetPath") ?? "/";
  // Same rule as the sign-in page: only a path on this site, never another origin.
  return /^\/(?![/\\])[^\\\r\n]*$/.test(target) ? base + target : base + "/";
};

const cookiePairs = (headers) => headers.getSetCookie().map((cookie) => cookie.split(";")[0]);

async function startLogin(query) {
  const csrf = await fetch(`${web}/api/auth/csrf`);
  const { csrfToken } = await csrf.json();
  const signIn = await fetch(`${web}/api/auth/signin/custom`, {
    method: "POST",
    redirect: "manual",
    headers: {
      "content-type": "application/x-www-form-urlencoded",
      cookie: cookiePairs(csrf.headers).join("; "),
    },
    body: new URLSearchParams({ csrfToken, callbackUrl: callbackUrl(query) }),
  });
  const location = signIn.headers.get("location") ?? "";
  if (!location.startsWith(`${issuer}/`)) throw new Error(`unexpected sign-in response ${signIn.status}`);
  return {
    location,
    cookies: [...csrf.headers.getSetCookie(), ...signIn.headers.getSetCookie()],
  };
}

// A failed login comes back with ?error=. The real page (or its props) shows it, so a failure cannot become a redirect loop.
async function showSignInPage(req, res, url) {
  const page = await fetch(web + url.pathname + url.search, {
    headers: {
      accept: req.headers.accept ?? "text/html",
      cookie: req.headers.cookie ?? "",
      // Next.js answers a props request only when the router marks it as one.
      ...(req.headers["x-nextjs-data"] ? { "x-nextjs-data": req.headers["x-nextjs-data"] } : {}),
    },
    redirect: "manual",
  });
  const headers = { "content-type": page.headers.get("content-type") ?? "text/html" };
  if (page.headers.get("location")) headers.location = page.headers.get("location");
  const cookies = page.headers.getSetCookie();
  if (cookies.length) headers["set-cookie"] = cookies;
  res.writeHead(page.status, headers);
  res.end(Buffer.from(await page.arrayBuffer()));
}

// The props request the router makes for /auth/sign-in (with the locale segment Langfuse's i18n adds). The build id is not checked here: a stale one
// only means the request never came from the current app, and the login it starts is the same one.
const SIGN_IN_DATA = /^\/_next\/data\/[A-Za-z0-9_-]{1,128}\/(?:[a-z]{2}(?:-[A-Za-z]{2})?\/)?auth\/sign-in\.json$/;

const sendData = (res, pageProps, cookies = []) => {
  const headers = { "content-type": "application/json", "cache-control": "no-store" };
  if (cookies.length) headers["set-cookie"] = cookies;
  res.writeHead(200, headers).end(JSON.stringify({ pageProps, __N_SSP: true }));
};

createServer(async (req, res) => {
  const url = new URL(req.url, "http://gateway");
  const page = url.pathname === "/auth/sign-in";
  const data = SIGN_IN_DATA.test(url.pathname);
  try {
    if (url.pathname === "/healthz") {
      res.writeHead(200).end("ok");
    } else if ((!page && !data) || (req.method !== "GET" && req.method !== "HEAD")) {
      res.writeHead(404).end();
    } else if (url.searchParams.has("error")) {
      await showSignInPage(req, res, url);
    } else if (data && (req.headers.purpose === "prefetch" || req.headers["x-middleware-prefetch"])) {
      // A prefetch must not start a login (it would replace the state cookies of a real one).
      res.writeHead(204, { "cache-control": "no-store" }).end();
    } else {
      const { location, cookies } = await startLogin(url.searchParams);
      if (data) sendData(res, { __N_REDIRECT: location, __N_REDIRECT_STATUS: 307 }, cookies);
      else res.writeHead(302, { location, "set-cookie": cookies, "cache-control": "no-store" }).end();
    }
  } catch (error) {
    console.error("sign-in gateway:", error.message);
    const failed = `${base}/auth/sign-in?error=OAuthSignin`;
    if (data) sendData(res, { __N_REDIRECT: failed, __N_REDIRECT_STATUS: 307 });
    else res.writeHead(302, { location: failed, "cache-control": "no-store" }).end();
  }
}).listen(3001);
