#!/usr/bin/env python3
"""Probe: is any nitter-style mirror reachable (optionally behind an Anubis PoW
challenge) so the X feed has a second, credential-free source?"""
import hashlib, json, re, time, urllib.error, urllib.parse, urllib.request

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
INSTANCES = ["nitter.tiekoetter.com", "xcancel.com", "nitter.privacydev.net",
             "lightbrd.com", "nitter.net", "nitter.poast.org", "nitter.space",
             "nitter.1d4.us", "twiiit.com"]
PATH = "/fullportnik/rss"


def get(url, cookies=None, timeout=25):
    h = {"User-Agent": UA, "Accept": "*/*", "Accept-Language": "en-US,en;q=0.9"}
    if cookies:
        h["Cookie"] = cookies
    req = urllib.request.Request(url, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, dict(r.headers), r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers or {}), e.read().decode("utf-8", "replace")
    except Exception as e:
        return 0, {}, f"{type(e).__name__}: {e}"


def solve_anubis(html, base):
    """Anubis: find nonce s.t. sha256(challenge + nonce)[:difficulty] is all zeros."""
    m = re.search(r'id="anubis_challenge" type="application/json">(.*?)</script>', html, re.S)
    if not m:
        return None
    ch = json.loads(m.group(1))
    rules = ch.get("rules") or {}
    c = ch.get("challenge") or {}
    difficulty = int(rules.get("difficulty") or 4)
    pre = str(c.get("randomData") or "")
    if not pre or c.get("method") != "fast":
        return None
    target = "0" * difficulty
    t0 = time.time()
    for nonce in range(0, 20_000_000):
        d = hashlib.sha256((pre + str(nonce)).encode()).hexdigest()
        if d.startswith(target):
            elapsed = int((time.time() - t0) * 1000)
            q = urllib.parse.urlencode({
                "response": d, "nonce": str(nonce), "redir": PATH,
                "elapsedTime": str(max(elapsed, 1)),
            })
            return f"{base}/.within.website/x/cmd/anubis/api/pass-challenge?{q}"
    return None


for host in INSTANCES:
    base = f"https://{host}"
    code, hdrs, body = get(base + PATH)
    if code == 200 and "<rss" in body.lower():
        n = body.count("<item>")
        print(f"{host:26s} OK 200 rss items={n}")
        continue
    if "anubis" in body.lower() or "not a bot" in body.lower():
        url = solve_anubis(body, base)
        if not url:
            print(f"{host:26s} anubis challenge unsolvable"); continue
        code2, h2, b2 = get(url)
        cookie = (h2.get("Set-Cookie") or "").split(";")[0]
        code3, h3, b3 = get(base + PATH, cookies=cookie)
        ok = code3 == 200 and "<rss" in b3.lower()
        print(f"{host:26s} anubis solved pass={code2} cookie={'yes' if cookie else 'no'} "
              f"rss={code3} items={b3.count('<item>') if ok else 0}"
              f"{'' if ok else ' body=' + b3[:120].replace(chr(10),' ')}")
        continue
    print(f"{host:26s} http={code} size={len(body)} head={body[:80]!r}")
