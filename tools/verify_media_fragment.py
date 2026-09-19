"""用真实浏览器验证 Media Fragment URI 的片段播放能力。

为什么必须实测
--------------
M2 的整个架构建立在「免切片播放」之上：前端只给 `<video src="x.mp4#t=a,b">`，
由浏览器自己定位到片段，后端不需要 ffmpeg 实时切片。如果这个行为不成立，
M2 就要推倒重来（改为服务端切片 + 缓存），成本差一个量级。

用法：
    python tools/verify_media_fragment.py http://127.0.0.1:8765/S01E01.mp4
"""

from __future__ import annotations

import argparse
import json
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright

PROBE_JS = """
async (url) => {
  const video = document.createElement('video');
  video.preload = 'metadata';
  video.muted = true;
  document.body.appendChild(video);
  const result = { url };
  try {
    video.src = url;
    await new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error('load timeout')), 20000);
      video.addEventListener('loadedmetadata', () => {
        clearTimeout(timer);
        resolve();
      }, { once: true });
      video.addEventListener('error', () => {
        clearTimeout(timer);
        reject(new Error('media load failed'));
      }, { once: true });
    });
    // 给浏览器一点时间完成 Media Fragment 定位
    await new Promise((resolve) => setTimeout(resolve, 700));
    result.ok = true;
    result.currentTime = video.currentTime;
    result.duration = video.duration;
    result.videoWidth = video.videoWidth;
    result.videoHeight = video.videoHeight;
  } catch (err) {
    result.ok = false;
    result.error = String(err);
  } finally {
    video.remove();
  }
  return result;
}
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="验证 Media Fragment 播放行为")
    parser.add_argument("base_url", help="视频 URL（不含 # 片段部分）")
    parser.add_argument("--tolerance", type=float, default=0.5, help="允许的定位偏差（秒）")
    args = parser.parse_args(argv)

    cases = [
        ("完整视频（无片段参数）", args.base_url, None),
        ("#t=2.38 起点", f"{args.base_url}#t=2.38", 2.38),
        ("#t=2.38,4.84 区间", f"{args.base_url}#t=2.38,4.84", 2.38),
        ("#t=754.2 中段", f"{args.base_url}#t=754.2", 754.2),
    ]

    origin = f"{urlsplit(args.base_url).scheme}://{urlsplit(args.base_url).netloc}"
    results = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            channel="chrome",
            headless=True,
            args=["--autoplay-policy=no-user-gesture-required"],
        )
        page = browser.new_page()
        # 关键：必须停在同源页面。about:blank 的 origin 是 null，
        # 从那里加载 http 媒体会被浏览器拒绝（表现为 media load failed）。
        page.goto(origin + "/")

        for label, url, expected in cases:
            info = page.evaluate(PROBE_JS, url)
            if not info.get("ok"):
                print(f"[FAIL] {label}")
                print(f"       错误: {info.get('error')}")
                results.append((label, False))
                continue

            detail = (
                f"currentTime={info['currentTime']:.3f}s  "
                f"duration={info['duration']:.1f}s  "
                f"{info['videoWidth']}x{info['videoHeight']}"
            )
            if expected is None:
                print(f"[ OK ] {label}")
                print(f"       {detail}")
                results.append((label, True))
                continue

            delta = abs(info["currentTime"] - expected)
            hit = delta <= args.tolerance
            verdict = "精确命中" if hit else f"偏差 {delta:.3f}s"
            print(f"[{' OK ' if hit else 'FAIL'}] {label}")
            print(f"       {detail}")
            print(f"       期望起点 {expected}s -> {verdict}")
            results.append((label, hit))

        browser.close()

    passed = sum(1 for _, ok in results if ok)
    print(f"\n结果: {passed}/{len(results)} 通过")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
