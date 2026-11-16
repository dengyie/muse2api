# -*- coding: utf-8 -*-
"""Video-only E2E via API (reuse existing first frame PNG). Idem key unique."""
import base64, hashlib, json, os, struct, sys, time, urllib.request, urllib.error

BASE = "http://127.0.0.1:18610"
KEY = os.environ.get("MUSE2API_KEY") or open("/opt/muse2api/repo/.env").read().split("MUSE2API_KEY=")[1].split("\n")[0].strip()
IDEM_KEY = "patch22e-vidonly-" + hashlib.sha256(open("/tmp/e2e_first_frame.png","rb").read()).hexdigest()[:12]
PROMPT = ('Native image-to-video (I2V) only. Animate first-frame character: jumping. '
          'One controlled low hop. Bend the existing support paws at the joints, push off, '
          'rise by 18 to 22 percent of frame height, hold airborne, and land at the exact '
          'baseline. For this 720p request, root travel is 130-to-158 pixels. Keep the '
          'existing leg lengths; bend only the support joints. Keep body and limbs unchanged; '
          'no rearing or hair spreading. Keep the head, face, and hair silhouette close to the '
          'reference pose; no eyelid closure. Leave at least 20 percent empty space above the '
          'highest body point in every frame; keep head and hair motion bounded. Reference '
          'identity data (appearance only; action instructions above take precedence): '
          '"Preserve the reference as the recognizable Doro pink-haired quadruped meme pet. '
          'Exact same oversized pink-haired chibi head, pink eyes, tiny mischievous mouth, '
          'hair ornaments, thick long side locks, tiny plump white animal torso and exactly '
          'four short white support legs. All four legs remain animal legs, never become '
          'extra human arms. Keep clean flat cel shading and smooth bold outlines. Full-body". '
          'Keep identity, anatomy, limb count and proportions; natural joint motion only. '
          'Locked camera, one full-body subject, uniform chroma green #12ff4d. Match '
          'start/end poses; clear margins. No Python, FFmpeg, SVG, static transforms, camera '
          'pan/zoom, slideshows or code animation. Create the jump only through natural '
          'joint-driven vertical body-root movement. If native I2V is unavailable, return a '
          'generation error instead of a substitute. Timing over 6s: 0-1s bend joints; 1-2s '
          'push off; 2-3.5s airborne peak hold; 3.5-5s land; 5-6s exact neutral at baseline.')


def api(method, path, payload=None, raw=False, headers=None, timeout=300):
    hh = {"Authorization": "Bearer " + KEY, "Content-Type": "application/json"}
    if headers:
        hh.update(headers)
    req = urllib.request.Request(BASE + path, method=method,
        data=json.dumps(payload).encode() if payload is not None else None, headers=hh)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            b = r.read(); return r.status, (b if raw else json.loads(b))
    except urllib.error.HTTPError as e:
        b = e.read()
        try: return e.code, json.loads(b)
        except Exception: return e.code, b[:300]


def log(m):
    print("[%s] %s" % (round(time.time() - T0, 1), m)); sys.stdout.flush()


T0 = time.time()
b64 = base64.b64encode(open("/tmp/e2e_first_frame.png", "rb").read()).decode()
print("first frame: %d bytes (from disk)" % len(open("/tmp/e2e_first_frame.png","rb").read()))
body = {"model": "muse-video", "prompt": PROMPT,
        "images": ["data:image/png;base64," + b64],
        "duration": 6, "aspect_ratio": "16:9", "resolution": "720p"}
log("submitting video-only I2V ...")
st, t = api("POST", "/v1/videos", body, headers={"Idempotency-Key": IDEM_KEY})
assert st in (200, 202), (st, t)
tid = t["id"]
log("accepted: %s" % tid)
last = ""
for i in range(160):
    st, t = api("GET", "/v1/videos/" + tid)
    sig = "%s|%s|%s" % (t["status"], t.get("stage") or "", t.get("progress") or "")
    if sig != last:
        log("poll#%d status=%s stage=%s progress=%s" % (i, t["status"], t.get("stage"), t.get("progress")))
        last = sig
    if t["status"] in ("completed", "failed"):
        break
    time.sleep(5)
print(json.dumps({k: t.get(k) for k in ("status","stage","error","error_code","generation_accepted","safe_to_resubmit","attempt_events","total_elapsed","url","duration")}, ensure_ascii=False, indent=1))
if t["status"] == "failed":
    sys.exit(2)
st, blob = api("GET", "/v1/videos/%s/content" % tid, raw=True)
assert st == 200
boxes, i = [], 0
while i + 8 <= len(blob):
    size = struct.unpack(">I", blob[i:i+4])[0]; typ = blob[i+4:i+8].decode("latin-1")
    boxes.append(typ)
    if size < 8: break
    i += size if size else len(blob)
sha = hashlib.sha256(blob).hexdigest()
log("content: %d bytes sha=%s boxes=%s" % (len(blob), sha[:16], boxes[:6]))
json.dump({"task_id": tid, "key": IDEM_KEY, "bytes": len(blob), "sha256": sha,
           "duration": t.get("duration"), "attempt_events": t.get("attempt_events"),
           "total_elapsed": t.get("total_elapsed"), "url": t.get("url"),
           "ftyp": "ftyp" in boxes, "moov": "moov" in boxes, "mdat": "mdat" in boxes},
          open("/tmp/e2e_result.json", "w"), indent=1)
print("VIDEONLY E2E DONE")
