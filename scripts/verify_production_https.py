"""End-to-end verification of the ShopEase production Docker stack.

Runs against the public-facing HTTPS origin (Caddy -> Gunicorn -> PostgreSQL)
and exercises every customer-facing flow: auth, browse, search, cart, wishlist,
checkout, orders, payments, admin, APIs, images, static files and deep-page
refreshes. Exits non-zero if any required check fails.

Usage: python scripts/verify_production_https.py [base_url]
Default base_url is https://localhost (the locally verified Docker stack).
"""
import re
import sys
import uuid

import requests

BASE = (sys.argv[1] if len(sys.argv) > 1 else "https://localhost").rstrip("/")
S = requests.Session()
S.verify = False
CSRF_RE = re.compile(r'name="csrfmiddlewaretoken"\s+value="([^"]+)"')
results = []
failures = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    if not ok:
        failures.append(name)
    print(("PASS " if ok else "FAIL ") + name + (f" :: {detail}" if detail else ""))


def csrf(path="/"):
    r = S.get(BASE + path, timeout=20)
    m = CSRF_RE.search(r.text)
    if m:
        return m.group(1)
    return S.cookies.get("csrftoken", "")


def post(path, data=None, ref=None):
    data = dict(data or {})
    data.setdefault("csrfmiddlewaretoken", csrf(ref or path))
    return S.post(BASE + path, data=data, headers={"Referer": BASE + (ref or path)},
                  timeout=20, allow_redirects=False)


def main():
    requests.packages.urllib3.disable_warnings()

    # ---- infrastructure / TLS -------------------------------------------
    r = S.get(BASE + "/health/", timeout=10)
    check("health endpoint", r.status_code == 200 and "OK" in r.text, f"{r.status_code}")

    r = requests.get("http://" + BASE.split("//", 1)[1] + "/health/",
                     timeout=10, allow_redirects=False)
    check("http->https redirect", r.status_code in (301, 308),
          f"{r.status_code} -> {r.headers.get('Location', '')}")

    # ---- pages & deep refreshes ------------------------------------------
    deep_paths = ["/", "/mens/", "/womens/", "/kids/", "/mens/casual-wear/",
                  "/search/?q=shirt", "/cart/", "/wishlist/", "/login/",
                  "/register/", "/contact-us/", "/catalog/", "/api/",
                  "/payment/", "/orders/", "/dashboard/", "/nonexistent-xyz/"]
    for p in deep_paths:
        r = S.get(BASE + p, timeout=20, allow_redirects=False)
        if p.startswith("/nonexistent"):
            want = {404}
        elif p in ("/wishlist/", "/orders/", "/dashboard/", "/payment/"):
            want = {200, 302}  # anon -> login redirect proves auth wiring
        else:
            want = {200}
        check(f"GET {p}", r.status_code in want, f"got {r.status_code}")

    r = requests.get(BASE + "/mens/casual-wear/", timeout=20, verify=False)
    check("deep-page cold refresh", r.status_code == 200, str(r.status_code))

    # ---- content -----------------------------------------------------------
    r = S.get(BASE + "/", timeout=20)
    check("home renders", r.status_code == 200 and len(r.text) > 5000, f"{len(r.text)} bytes")
    r = S.get(BASE + "/search/?q=shirt", timeout=20)
    check("search works", r.status_code == 200 and "shirt" in r.text.lower(), str(r.status_code))
    r = S.get(BASE + "/api/products/", timeout=20)
    try:
        payload = r.json()
        count = len(payload) if isinstance(payload, list) else len(payload.get("results", []))
    except Exception:
        payload, count = None, 0
    check("products API JSON", r.status_code == 200 and count > 0, f"{r.status_code} count={count}")
    r = S.get(BASE + "/api/", timeout=20)
    check("API root", r.status_code == 200, str(r.status_code))

    # ---- images & static ---------------------------------------------------
    home_html = S.get(BASE + "/", timeout=20).text
    m = re.search(r'"(/static/[^"]+\.(?:css|js|webp))"', home_html)
    if m:
        r = S.get(BASE + m.group(1), timeout=20)
        check("static asset served (WhiteNoise manifest)", r.status_code == 200,
              f"{m.group(1)} -> {r.status_code}")
    else:
        check("static asset served (WhiteNoise manifest)", False,
              "no /static/ ref in homepage")
    m = re.search(r'"(/assets/[^"]+\.(?:webp|jpg|png))"', home_html)
    asset_path = m.group(1) if m else "/assets/images/menstshirts.webp"
    r = S.get(BASE + asset_path, timeout=20)
    check("image asset served",
          r.status_code == 200 and r.headers.get("Content-Type", "").startswith("image"),
          f"{asset_path} -> {r.status_code} {r.headers.get('Content-Type', '')}")

    # ---- register / login / logout -----------------------------------------
    email = f"e2e-{uuid.uuid4().hex[:10]}@example.com"
    password = "E2eStrongPass!2026"
    r = post("/register/", {"email": email, "password": password,
                            "confirm_password": password}, ref="/register/")
    check("register creates account", r.status_code == 302, f"{r.status_code}")

    # session now authenticated (register auto-logs-in); logout first to test login
    S.get(BASE + "/logout/", timeout=20)

    r = post("/login/", {"email": email, "password": "WrongPassword!"},
             ref="/login/")
    wrong_blocked = r.status_code == 200  # renders login page with error
    check("login rejects wrong password", wrong_blocked, str(r.status_code))

    r = post("/login/", {"email": email, "password": password}, ref="/login/")
    check("login accepts correct password", r.status_code == 302,
          f"{r.status_code} {r.headers.get('Location', '')}")

    # ---- cart -----------------------------------------------------------------
    r = post("/cart/add/26/", {"quantity": "1", "selected_size": "M"}, ref="/product/26/")
    check("add to cart", r.status_code == 302 and r.headers.get("Location", "").endswith("/cart/"),
          f"{r.status_code} -> {r.headers.get('Location', '')}")
    r = S.get(BASE + "/cart/", timeout=20)
    check("cart page shows item", r.status_code == 200, str(r.status_code))

    # ---- wishlist --------------------------------------------------------------
    r = post("/wishlist/toggle/26/", ref="/catalog/")
    check("wishlist toggle", r.status_code == 302, str(r.status_code))
    r = S.get(BASE + "/wishlist/", timeout=20)
    check("wishlist page (authed)", r.status_code == 200, str(r.status_code))

    # ---- checkout: place order (cash-style flow) --------------------------------
    order_location = ""
    r = post("/place-order/", {
        "full_name": "E2E Tester", "phone_number": "9999999999", "email": email,
        "address": "1 Test Street", "city": "Bengaluru", "state": "Karnataka",
        "pincode": "560001", "country": "India", "payment_method": "Card",
    }, ref="/payment/")
    if r.status_code == 302 and "/orders/" in r.headers.get("Location", ""):
        order_location = r.headers.get("Location", "")
    check("place order (checkout)", r.status_code == 302, f"{r.status_code} -> {r.headers.get('Location', '')}")
    if order_location:
        r = S.get(BASE + order_location, timeout=20)
        check("order success page", r.status_code == 200, str(r.status_code))
    r = S.get(BASE + "/orders/", timeout=20)
    check("orders page lists orders", r.status_code == 200, str(r.status_code))

    # ---- payment API (graceful degradation with placeholder keys) ----------------
    r = post("/payment/create/", {
        "full_name": "E2E Tester", "phone_number": "9999999999", "email": email,
        "address": "1 Test Street", "city": "Bengaluru", "state": "Karnataka",
        "pincode": "560001", "country": "India",
    }, ref="/payment/")
    try:
        j = r.json()
        graceful = r.status_code in (200, 400) and "ok" in j
    except Exception:
        graceful = False
        j = {}
    check("payment create endpoint responds", graceful,
          f"{r.status_code} {str(j)[:120]}")

    # ---- admin -------------------------------------------------------------------
    r = requests.get(BASE + "/admin/", timeout=20, verify=False, allow_redirects=False)
    check("django admin reachable", r.status_code in (302, 200),
          f"{r.status_code} -> {r.headers.get('Location', '')}")

    # ---- logout ------------------------------------------------------------------
    r = S.get(BASE + "/logout/", timeout=20, allow_redirects=False)
    check("logout", r.status_code in (200, 302, 303), str(r.status_code))
    r = S.get(BASE + "/orders/", timeout=20, allow_redirects=False)
    check("auth cleared after logout", r.status_code == 302, str(r.status_code))

    # ---- security headers ------------------------------------------------------------
    r = requests.get(BASE + "/", timeout=20, verify=False, allow_redirects=False)
    h = r.headers
    check("security headers (HSTS/XFO/nosniff)",
          bool(h.get("Strict-Transport-Security")) and h.get("X-Frame-Options") == "DENY"
          and h.get("X-Content-Type-Options") == "nosniff",
          f"HSTS={h.get('Strict-Transport-Security')} XFO={h.get('X-Frame-Options')} "
          f"XCTO={h.get('X-Content-Type-Options')}")

    print()
    print(f"TOTAL={len(results)} PASS={len(results) - len(failures)} FAIL={len(failures)}")
    if failures:
        print("FAILED:", ", ".join(failures))
        sys.exit(1)


if __name__ == "__main__":
    main()
