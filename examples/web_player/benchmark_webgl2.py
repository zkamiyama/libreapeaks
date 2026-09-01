"""Headless-browser smoke/perf probe for the packed WebGL2 player path."""
from __future__ import annotations

import argparse
import json
import re
import time

from selenium import webdriver
from selenium.webdriver.support.ui import WebDriverWait


def upload_count(text: str) -> int:
    match = re.search(r"\buploads=(\d+)\b", text)
    if not match:
        raise RuntimeError(f"renderer diagnostics missing upload count: {text!r}")
    return int(match.group(1))


def raw_resource_stats(driver) -> dict[str, float | int]:
    return driver.execute_script(
        """
        const rows = performance.getEntriesByType('resource')
          .filter(e => e.name.includes('/api/gpu-records'));
        return {
          requests: rows.length,
          encoded_body_bytes: rows.reduce((n, e) => n + (e.encodedBodySize || 0), 0),
          transferred_bytes: rows.reduce((n, e) => n + (e.transferSize || 0), 0),
          duration_ms: rows.reduce((n, e) => n + (e.duration || 0), 0),
        };
        """
    )


def pcm_resource_count(driver) -> int:
    return int(
        driver.execute_script(
            """
            return performance.getEntriesByType('resource')
              .filter(e => e.name.includes('/api/pcm-window')).length;
            """
        )
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url")
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args()

    options = webdriver.ChromeOptions()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--enable-webgl")
    options.add_argument("--ignore-gpu-blocklist")
    options.add_argument("--enable-unsafe-swiftshader")
    options.add_argument("--use-angle=swiftshader")
    options.add_argument("--window-size=1600,900")
    options.set_capability("goog:loggingPrefs", {"browser": "ALL"})

    driver = webdriver.Chrome(options=options)
    try:
        driver.get(args.url)
        wait = WebDriverWait(driver, args.timeout)
        wait.until(
            lambda d: d.execute_script(
                "return document.documentElement.dataset.webgl2Ready === '1'"
            )
        )
        wait.until(
            lambda d: "WebGL2 packed"
            in d.find_element("id", "rendererInfo").text
        )
        # Give the post-initial-render ResizeObserver callback one frame to fire.
        # Identical resident windows must be reused rather than fetched again.
        time.sleep(0.25)

        initial = driver.find_element("id", "rendererInfo").text
        initial_uploads = upload_count(initial)
        initial_resources = raw_resource_stats(driver)
        if initial_resources["requests"] != initial_uploads:
            raise RuntimeError(
                "duplicate initial raw requests detected: "
                f"requests={initial_resources['requests']} uploads={initial_uploads}"
            )

        # Uniform-only stress: gain, heatmap and vertical full-scale should not
        # cause raw record network traffic once the current windows are resident.
        uniform_start = time.perf_counter()
        for index in range(40):
            gain = 0.2 + (index % 20) * 0.2
            driver.execute_script(
                """
                const el = document.getElementById('spectrogramGain');
                el.value = arguments[0];
                el.dispatchEvent(new Event('input', {bubbles: true}));
                """,
                gain,
            )
        for index in range(20):
            driver.execute_script(
                """
                const stack = document.getElementById('webglStack');
                stack.dispatchEvent(new WheelEvent('wheel', {
                    deltaY: arguments[0], ctrlKey: true, clientX: 800,
                    bubbles: true, cancelable: true
                }));
                """,
                -120 if index % 2 == 0 else 120,
            )
        uniform_seconds = time.perf_counter() - uniform_start
        time.sleep(0.2)
        uniform_diagnostics = driver.find_element("id", "rendererInfo").text
        uniform_uploads = upload_count(uniform_diagnostics)
        uniform_resources = raw_resource_stats(driver)
        if uniform_uploads != initial_uploads:
            raise RuntimeError(
                "uniform-only controls unexpectedly uploaded raw records: "
                f"{initial_uploads} -> {uniform_uploads}"
            )
        if uniform_resources["requests"] != initial_resources["requests"]:
            raise RuntimeError(
                "uniform-only controls unexpectedly fetched raw records: "
                f"{initial_resources['requests']} -> {uniform_resources['requests']}"
            )

        # Only count raw endpoint traffic caused by horizontal viewport changes.
        driver.execute_script("performance.clearResourceTimings()")

        # Paged stress: horizontal wheel zoom bursts exercise raw record window
        # selection, HTTP transfer and texture replacement.
        paged_start = time.perf_counter()
        for index in range(18):
            driver.execute_script(
                """
                const stack = document.getElementById('webglStack');
                const rect = stack.getBoundingClientRect();
                stack.dispatchEvent(new WheelEvent('wheel', {
                    deltaY: arguments[0], clientX: rect.left + rect.width * arguments[1],
                    bubbles: true, cancelable: true
                }));
                """,
                -120 if index < 9 else 120,
                0.2 + (index % 5) * 0.15,
            )
            time.sleep(0.04)
        paged_seconds = time.perf_counter() - paged_start
        time.sleep(0.8)

        diagnostics = driver.find_element("id", "rendererInfo").text
        resource_stats = raw_resource_stats(driver)

        # Reach one-sample-per-record LOD. The debounce should collapse this
        # wheel burst to the final source request, and the resulting R32F
        # texture exercises the exact line/dot renderer in a real WebGL2
        # context.
        for _index in range(26):
            driver.execute_script(
                """
                const stack = document.getElementById('webglStack');
                const rect = stack.getBoundingClientRect();
                stack.dispatchEvent(new WheelEvent('wheel', {
                    deltaY: -120, clientX: rect.left + rect.width * 0.5,
                    bubbles: true, cancelable: true
                }));
                """
            )
            time.sleep(0.015)
        wait.until(
            lambda d: "PCM samples"
            in d.find_element("id", "rendererInfo").text
        )
        sample_diagnostics = driver.find_element("id", "rendererInfo").text
        range_diagnostics = driver.find_element("id", "pcmRangeInfo").text
        pcm_requests = pcm_resource_count(driver)
        if pcm_requests < 1:
            raise RuntimeError("exact sample LOD made no source PCM request")
        decode_match = re.search(r"\bdecoded=(\d+)\b", range_diagnostics)
        if (
            decode_match is None
            or int(decode_match.group(1)) < 1
            or "last decode #" not in range_diagnostics
        ):
            raise RuntimeError(
                f"range-decode debug notification is missing: {range_diagnostics!r}"
            )
        info = driver.execute_script(
            """
            const canvas = document.getElementById('analysisGl');
            const gl = canvas.getContext('webgl2');
            if (!gl) return {webgl2: false};
            const dbg = gl.getExtension('WEBGL_debug_renderer_info');
            return {
              webgl2: true,
              renderer: dbg ? gl.getParameter(dbg.UNMASKED_RENDERER_WEBGL) : gl.getParameter(gl.RENDERER),
              vendor: dbg ? gl.getParameter(dbg.UNMASKED_VENDOR_WEBGL) : gl.getParameter(gl.VENDOR),
              version: gl.getParameter(gl.VERSION),
              timer_query: !!gl.getExtension('EXT_disjoint_timer_query_webgl2'),
              max_texture_size: gl.getParameter(gl.MAX_TEXTURE_SIZE),
              width: canvas.width,
              height: canvas.height,
              backend: document.documentElement.dataset.renderer,
              ready: document.documentElement.dataset.webgl2Ready,
            };
            """
        )
        errors = [
            row
            for row in driver.get_log("browser")
            if row.get("level") in {"SEVERE", "ERROR"}
            and "/favicon.ico" not in row.get("message", "")
        ]
        result = {
            **info,
            "initial": initial,
            "initial_uploads": initial_uploads,
            "initial_raw_resources": initial_resources,
            "uniform_diagnostics": uniform_diagnostics,
            "uniform_uploads": uniform_uploads,
            "uniform_raw_resources": uniform_resources,
            "diagnostics": diagnostics,
            "sample_diagnostics": sample_diagnostics,
            "range_diagnostics": range_diagnostics,
            "pcm_requests": pcm_requests,
            "uniform_stress_ms": uniform_seconds * 1000.0,
            "paged_stress_ms": paged_seconds * 1000.0,
            "paged_raw_resources": resource_stats,
            "browser_errors": errors,
        }
        print("WEBGL2_BROWSER_BENCH " + json.dumps(result, sort_keys=True))
        if not info.get("webgl2") or info.get("backend") != "webgl2":
            return 2
        if errors:
            return 3
        return 0
    finally:
        driver.quit()


if __name__ == "__main__":
    raise SystemExit(main())
