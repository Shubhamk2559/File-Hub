/**
 * hub-router.js - ONE domain (https://hubpa.com) in front of ALL bot servers.
 *
 *   hubpa.com/<filecode>            -> any healthy server -> redirects to t.me/<bot>?start=<filecode>
 *   hubpa.com/dl/<token>/<msg_id>   -> any healthy server -> streams / downloads the file (Range supported)
 *
 * Settings (Worker -> Settings -> Variables):
 *   ORIGINS   comma separated server URLs, no trailing slash:
 *             https://a.koyeb.app,https://b.koyeb.app,https://c.koyeb.app,https://d.onrender.com
 *   DL_MODE   "proxy"    (default) file bytes flow through Cloudflare, the user only ever sees hubpa.com
 *             "redirect" the user is sent to the chosen server's own URL for /dl/... (saves Cloudflare traffic)
 *   CACHE_LINKS  optional, seconds. e.g. 30 = the t.me redirect of a file link is cached at Cloudflare for 30 s, so a link
 *             opened by hundreds of people reaches your servers only once per 30 s. 0 / not set = off.
 *             (A revoked link keeps working for at most that many seconds.)
 *
 * Speed-ups in this version:
 *   * the landing page "/" and /assets/* are cached at Cloudflare (5 min / 1 h): the servers never see most visitors
 *   * pages fail over after 8 s (was 15 s), so a sleeping free server costs the visitor a few seconds, not 15
 */
const BLOCKED = [/^\/tg\//, /^\/internal\//, /^\/api\//, /^\/monetag\//];   // never expose these through the domain

function parseOrigins(env) {
  return String(env.ORIGINS || "").split(",").map(s => s.trim().replace(/\/+$/, "")).filter(Boolean);
}

function shuffle(a) {
  for (let i = a.length - 1; i > 0; i--) {
    const j = Math.floor(Math.random() * (i + 1));
    [a[i], a[j]] = [a[j], a[i]];
  }
  return a;
}

// servers that failed recently are tried last (this isolate only, resets by itself)
const down = new Map();
function order(origins) {
  const now = Date.now();
  const ok = origins.filter(o => (down.get(o) || 0) < now);
  const bad = origins.filter(o => (down.get(o) || 0) >= now);
  return shuffle(ok).concat(shuffle(bad));
}

const FILE_CODE = /^\/[A-Za-z0-9_-]{6,64}$/;

function edgeTtl(url, env) {
  if (url.pathname === "/health") return 0;          // live status, never cached (also looks like a file code)
  if (url.pathname === "/") return 300;
  if (url.pathname.startsWith("/assets/")) return 3600;
  if (FILE_CODE.test(url.pathname)) return Math.max(0, parseInt(env.CACHE_LINKS || "0", 10) || 0);
  return 0;
}

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    if (BLOCKED.some(r => r.test(url.pathname))) return new Response("Not found", { status: 404 });
    if (request.method !== "GET" && request.method !== "HEAD") return new Response("Method not allowed", { status: 405 });

    const origins = parseOrigins(env);
    if (!origins.length) return new Response("ORIGINS is not set in the Worker", { status: 500 });

    // ---- edge cache (GET only): hits never reach the servers
    const ttl = request.method === "GET" ? edgeTtl(url, env) : 0;
    const cache = caches.default;
    const cacheKey = new Request(url.origin + url.pathname + url.search, { method: "GET" });
    if (ttl) {
      const hit = await cache.match(cacheKey);
      if (hit) {
        const h = new Response(hit.body, hit);
        h.headers.set("X-Cache", "HIT");
        return h;
      }
    }

    const isDl = url.pathname.startsWith("/dl/");
    const redirectMode = isDl && String(env.DL_MODE || "proxy").toLowerCase() === "redirect";
    let lastStatus = 502;

    for (const origin of order(origins)) {
      const target = origin + url.pathname + url.search;
      try {
        if (redirectMode) {
          // quick check that this server is alive before sending the user there
          const probe = await fetch(origin + "/health", { method: "GET", signal: AbortSignal.timeout(6000) });
          if (!probe.ok) throw new Error("unhealthy " + probe.status);
          return Response.redirect(target, 302);
        }
        const headers = new Headers();
        for (const h of ["range", "if-range", "accept", "accept-encoding", "user-agent"]) {
          const v = request.headers.get(h);
          if (v) headers.set(h, v);
        }
        const res = await fetch(target, { method: request.method, headers, redirect: "manual",
                                          signal: AbortSignal.timeout(isDl ? 25000 : 8000) });
        if (res.status >= 500) { lastStatus = res.status; down.set(origin, Date.now() + 30000); continue; }
        const out = new Response(res.body, res);          // passes 200/206/302/404 + Range headers unchanged
        out.headers.set("X-Served-By", new URL(origin).hostname.split(".")[0]);
        if (ttl && (res.status === 200 || res.status === 302)) {
          out.headers.set("Cache-Control", `public, max-age=${ttl}`);
          out.headers.set("X-Cache", "MISS");
          ctx.waitUntil(cache.put(cacheKey, out.clone()));
        }
        return out;
      } catch (e) {
        down.set(origin, Date.now() + 30000);              // sleeping / dead server -> next one
      }
    }
    return new Response("All servers are busy or waking up, please retry in a few seconds.", {
      status: lastStatus === 502 ? 503 : lastStatus, headers: { "Retry-After": "10" } });
  },
};
