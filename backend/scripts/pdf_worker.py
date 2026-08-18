"""PDF 工作进程：通过子进程生成 PDF，规避 Windows ProactorEventLoop 限制"""
import sys, json
from playwright.sync_api import sync_playwright

if __name__ == "__main__":
    html_path = sys.argv[1]
    output_path = sys.argv[2]

    with open(html_path, "r", encoding="utf-8") as f:
        html = f.read()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(html, wait_until="networkidle")
        page.pdf(path=output_path, format="A4", print_background=True)
        browser.close()

    print(f"PDF generated: {output_path}")
