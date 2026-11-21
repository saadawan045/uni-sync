"""Diagnose portal login without a browser. Prints no credentials."""
import re
import sys
import tomllib
from pathlib import Path

import requests
from bs4 import BeautifulSoup

cfg = tomllib.loads((Path.home() / ".config/uni-sync/config.toml").read_text())["portal"]
url = cfg.get("login_url", "https://nit.effii.app/portal/")
u, p = cfg.get("username", ""), cfg.get("password", "")
print(f"username: {len(u)} chars, starts {u[:3]!r}, has spaces: {u != u.strip() or ' ' in u}")
print(f"password: {len(p)} chars, has leading/trailing spaces: {p != p.strip()}")

s = requests.Session()
s.headers["User-Agent"] = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")
r = s.get(url, timeout=30)
soup = BeautifulSoup(r.text, "lxml")
form = soup.find("form", id="LLForm") or soup.find("form")
data = {i.get("name"): i.get("value", "") for i in form.find_all("input") if i.get("name")}
data["gua_cnic"], data["user_password"] = u, p
print("form fields:", [k for k in data])
print("cookies after GET:", list(s.cookies.keys()))

r2 = s.post(url, data=data, timeout=30, headers={"Referer": url, "Origin": "https://nit.effii.app"})
print("\nPOST status:", r2.status_code, "| redirects:", [h.status_code for h in r2.history], "| final URL:", r2.url)
s2 = BeautifulSoup(r2.text, "lxml")
print("page title:", s2.title.get_text(strip=True) if s2.title else None)
print("still shows login form:", bool(s2.find("input", attrs={"name": "user_password"})))
for t in s2(["script", "style"]):
    t.decompose()
text = re.sub(r"\s+", " ", s2.get_text(" "))
hits = re.findall(r".{0,70}(?:invalid|incorrect|wrong|error|not found|denied|blocked|attempt|expired|inactive|disabled).{0,70}", text, re.I)
print("error-like text:", hits[:5] or "none")
# also look inside scripts for alert()/swal() messages
alerts = re.findall(r"(?:alert|swal|toastr\.\w+)\s*\(\s*['\"]([^'\"]{3,120})", r2.text)
print("script messages:", alerts[:5] or "none")
print("links on result page:", [a.get_text(strip=True) for a in s2.find_all("a") if a.get_text(strip=True)][:15])
