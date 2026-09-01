"""Headless-browser smoke/perf probe for the packed WebGL2 player path."""
from __future__ import annotations

import argparse
import json
import time

from selenium import webdriver
from selenium.webdriver.support.ui import WebDriverWait


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

        initial = driver.find_element("id", "rendererInfo").text
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

        # Paged stress: horizontal wheel zoom and pan-like wheel bursts exercise
        # raw record window selection, HTTP transfer and texture replacement.
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
        ]
        result = {
            **info,
            "initial": initial,
            "diagnostics": diagnostics,
            "uniform_stress_ms": uniform_seconds * 1000.0,
            "paged_stress_ms": paged_seconds * 1000.0,
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
