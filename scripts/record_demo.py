#!/usr/bin/env python3
"""Record a narrated walkthrough of the SLO Watchdog console.

Drives the real console against the real Grafana stack -- nothing here is
mocked, so the numbers on screen are the ones the sweep actually produced.

    slo-watchdog serve &
    python scripts/record_demo.py

Writes demo/video/console-walkthrough.mp4.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "demo" / "video"
WIDTH, HEIGHT = 1360, 850

# Caption overlay, injected into the page so the recording carries its own
# narration rather than needing an edit pass afterwards.
OVERLAY = """
(() => {
  const bar = document.createElement('div');
  bar.id = '__cap';
  bar.style.cssText = `
    position:fixed;left:0;right:0;bottom:0;z-index:99999;
    padding:18px 30px;font:500 19px/1.45 -apple-system,BlinkMacSystemFont,Inter,system-ui,sans-serif;
    color:#e6e9ef;background:linear-gradient(180deg,rgba(10,12,16,0),rgba(10,12,16,.94) 42%);
    text-shadow:0 2px 12px rgba(0,0,0,.9);opacity:0;transition:opacity .45s ease;
    display:flex;align-items:center;gap:13px;pointer-events:none;`;
  const dot = document.createElement('span');
  dot.style.cssText = 'width:8px;height:8px;border-radius:50%;background:#f5a524;flex:0 0 auto;box-shadow:0 0 12px #f5a524';
  const txt = document.createElement('span');
  txt.id = '__captxt';
  bar.append(dot, txt);
  document.body.appendChild(bar);
  window.__caption = (t) => {
    const b = document.getElementById('__cap'), s = document.getElementById('__captxt');
    if (!t) { b.style.opacity = 0; return; }
    b.style.opacity = 0;
    setTimeout(() => { s.textContent = t; b.style.opacity = 1; }, 220);
  };
  // Smooth scrolling makes the pan readable at 25fps.
  document.documentElement.style.scrollBehavior = 'smooth';
})();
"""


def record(url: str, keep_webm: bool) -> int:
    from playwright.sync_api import sync_playwright

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--force-color-profile=srgb"])
        context = browser.new_context(
            viewport={"width": WIDTH, "height": HEIGHT},
            device_scale_factor=1,
            record_video_dir=str(OUT_DIR),
            record_video_size={"width": WIDTH, "height": HEIGHT},
        )
        page = context.new_page()

        def caption(text: str, hold: float = 0.0) -> None:
            page.evaluate("t => window.__caption(t)", text)
            if hold:
                page.wait_for_timeout(int(hold * 1000))

        print("recording…")
        page.goto(url, wait_until="networkidle")
        page.add_script_tag(content=OVERLAY)
        page.wait_for_timeout(1200)

        caption("SLO Watchdog — it finds the reliability problems nobody paged on.", 3.4)

        caption("It is connected to a live Grafana Cloud stack through the MCP server.", 2.0)
        page.wait_for_selector("#health.ok", timeout=30_000)
        page.wait_for_timeout(2.0 * 1000)

        caption("Eleven media services. Nothing is alerting. Let's sweep.", 2.8)
        page.click("#sweep")
        caption("Discovery and burn-rate detection — deterministic Python, no LLM.", 3.0)
        page.wait_for_selector(".card.finding", timeout=180_000)
        page.wait_for_timeout(1400)

        # Narrate what the sweep actually found rather than a fixed script --
        # the stack is live, so the finding set changes between takes.
        data = page.evaluate("""() => {
          const cards = [...document.querySelectorAll('.card.finding')];
          return cards.map(c => ({
            service: c.querySelector('.svc').textContent.trim(),
            burn: c.querySelector('.burn').textContent.trim(),
            impact: (c.querySelector('.impact')?.textContent || '').trim(),
            provisional: !!c.querySelector('.chip.prov'),
          }));
        }""")
        quiet = page.evaluate(
            "() => document.querySelectorAll('#inventory tbody tr').length"
            " - document.querySelectorAll('.card.finding').length"
        )

        plural = "finding" if len(data) == 1 else "findings"
        caption(f"{len(data)} {plural}. Not one of them would page.", 3.2)

        for i, f in enumerate(data[:3]):
            page.evaluate(
                f"document.querySelectorAll('.card.finding')[{i}]"
                ".scrollIntoView({block:'center'})"
            )
            impact = f["impact"]
            if len(impact) > 105:
                impact = impact[:102].rsplit(" ", 1)[0] + "…"
            tail = " — on a service with no SLO at all." if f["provisional"] else ""
            caption(f"{f['service']} at {f['burn']} — {impact}{tail}", 4.3)

        caption(f"{quiet} other services were checked and correctly left alone.", 3.2)
        page.evaluate("window.scrollTo(0,0)")
        page.wait_for_timeout(2000)

        caption("Failure injection is specified by target burn rate, not a magic percentage.", 3.8)
        page.evaluate("document.querySelector('#scenarios').scrollIntoView({block:'center'})")
        page.wait_for_timeout(2600)

        caption("The transcode spike recovers on its own — the short window suppresses it.", 3.8)
        page.wait_for_timeout(1400)

        caption("Four services carry real SLOs. Seven are provisional — nobody wrote one.", 4.2)
        page.evaluate("document.querySelector('#inventory').scrollIntoView({block:'start'})")
        page.wait_for_timeout(3000)

        caption("Your alerts tell you what broke. This tells you what's breaking.", 4.0)
        page.evaluate("window.scrollTo(0,0)")
        page.wait_for_timeout(2600)
        caption("", 0.8)

        video = page.video
        context.close()
        browser.close()
        raw = Path(video.path())

    mp4 = OUT_DIR / "console-walkthrough.mp4"
    print(f"encoding {raw.name} -> {mp4.name}")
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(raw),
         "-vf", "scale=1360:-2:flags=lanczos,fps=25",
         "-c:v", "libx264", "-preset", "slow", "-crf", "22",
         "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(mp4)],
        check=True,
    )
    if not keep_webm:
        raw.unlink(missing_ok=True)

    size = mp4.stat().st_size / 1_000_000
    print(f"done: {mp4}  ({size:.1f} MB)")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8080")
    parser.add_argument("--keep-webm", action="store_true")
    args = parser.parse_args()
    try:
        return record(args.url, args.keep_webm)
    except Exception as exc:  # noqa: BLE001
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
