// BOXP-428: Langfuse's sign-in page has no setting that starts a provider on its own. Everyone who
// reaches this hostname already passed Cloudflare Access, so this answers /auth/sign-in by starting
// the Access OIDC login. Langfuse still checks the ID token; this holds no credential and trusts no header.
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

// A failed login comes back with ?error=. The real page shows it, so a failure cannot become a redirect loop.
async function showSignInPage(req, res, url) {
  const page = await fetch(web + url.pathname + url.search, {
    headers: { accept: req.headers.accept ?? "text/html", cookie: req.headers.cookie ?? "" },
    redirect: "manual",
  });
  const headers = { "content-type": page.headers.get("content-type") ?? "text/html" };
  if (page.headers.get("location")) headers.location = page.headers.get("location");
  const cookies = page.headers.getSetCookie();
  if (cookies.length) headers["set-cookie"] = cookies;
  res.writeHead(page.status, headers);
  res.end(Buffer.from(await page.arrayBuffer()));
}

createServer(async (req, res) => {
  const url = new URL(req.url, "http://gateway");
  try {
    if (url.pathname === "/healthz") {
      res.writeHead(200).end("ok");
    } else if (url.pathname !== "/auth/sign-in" || (req.method !== "GET" && req.method !== "HEAD")) {
      res.writeHead(404).end();
    } else if (false) {
      await showSignInPage(req, res, url);
    } else {
      const { location, cookies } = await startLogin(url.searchParams);
      res.writeHead(302, { location, "set-cookie": cookies, "cache-control": "no-store" }).end();
    }
  } catch (error) {
    console.error("sign-in gateway:", error.message);
    res.writeHead(302, { location: `${base}/auth/sign-in?error=OAuthSignin`, "cache-control": "no-store" }).end();
  }
}).listen(3001);
