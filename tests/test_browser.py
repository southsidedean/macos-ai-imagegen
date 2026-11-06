"""The page, in real browsers, against the real server and worker with the fake engine.

Python can't see the page's JavaScript, and cookies and Sec-Fetch headers differ between browsers. MIA_BROWSERS names which to
use, as in MIA_BROWSERS=chrome,firefox,safari. Without it these are skipped. Safari needs Allow Remote Automation, in the
Developer section of its settings, and can't run hidden. HTTPS is left to the others, as Safari won't accept mia's authority
unless the Mac trusts it.
"""

from __future__ import annotations

import dataclasses
import http.server
import json
import os
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, cast

import pytest
from selenium import webdriver
from selenium.common.exceptions import StaleElementReferenceException, WebDriverException
from selenium.webdriver.common.by import By
from selenium.webdriver.remote.webdriver import WebDriver
from selenium.webdriver.remote.webelement import WebElement
from selenium.webdriver.support.ui import Select, WebDriverWait
from test_smoke import Service, free_port

from mia import paths, remote

BROWSERS = [name for name in os.environ.get("MIA_BROWSERS", "").split(",") if name]
pytestmark = [pytest.mark.timeout(120), pytest.mark.skipif(not BROWSERS, reason="MIA_BROWSERS names no browsers")]
HOSTILE = '<img src=x onerror="window.pwned=1"><script>window.pwned=1</script>'
PIXEL = {"width": 512, "height": 512, "steps": 2}


def start_browser(name: str) -> WebDriver:
    if name == "chrome":
        chrome = webdriver.ChromeOptions()
        chrome.add_argument("--headless=new")
        chrome.add_argument("--no-proxy-server")
        chrome.set_capability("acceptInsecureCerts", True)
        return webdriver.Chrome(options=chrome)
    if name == "firefox":
        firefox = webdriver.FirefoxOptions()
        firefox.add_argument("-headless")
        firefox.set_capability("acceptInsecureCerts", True)
        return webdriver.Firefox(options=firefox)
    if name == "safari":
        return webdriver.Safari()
    raise AssertionError(f"{name} isn't a browser these tests know")


# Started before the fixture that fences off the network, because Selenium may have to fetch a browser's driver the first time.
@pytest.fixture(scope="module", params=BROWSERS)
def browser(request: pytest.FixtureRequest) -> Iterator[WebDriver]:
    driver = start_browser(request.param)
    try:
        yield driver
    finally:
        driver.quit()


@pytest.fixture
def service(tmp_path: Path) -> Iterator[Service]:
    running = Service(tmp_path / "mia")
    try:
        yield running
    finally:
        running.stop()


class Page:
    def __init__(self, driver: WebDriver, address: str) -> None:
        self.driver = driver
        self.address = address
        driver.get(address)
        driver.delete_all_cookies()  # the last test's server was on this host too, on another port
        driver.get(address)

    def find(self, element_id: str) -> WebElement:
        return self.driver.find_element(By.ID, element_id)

    def wait[T](self, check: Callable[[], T], seconds: float = 20) -> T:
        def attempt(_driver: WebDriver) -> T | bool:
            try:
                return check()
            except StaleElementReferenceException:  # the page redrew it: look again
                return False

        return cast("T", WebDriverWait(self.driver, seconds, 0.1).until(attempt))

    def shown(self, element_id: str) -> bool:
        return self.find(element_id).is_displayed()

    def run(self, script: str) -> Any:
        return self.driver.execute_script(script)

    def type(self, element_id: str, text: str) -> None:
        field = self.find(element_id)
        field.clear()
        field.send_keys(text)

    def click(self, element_id: str) -> None:
        self.driver.execute_script("arguments[0].click()", self.find(element_id))

    def sign_in(self, token: str) -> None:
        self.wait(lambda: self.shown("sign-in-view"))
        self.type("token", token)
        self.click_submit("sign-in-form")
        self.wait(lambda: self.shown("main-view"))

    def click_submit(self, form_id: str) -> None:
        self.driver.execute_script("arguments[0].requestSubmit()", self.find(form_id))

    def thumbnails(self) -> list[WebElement]:
        return self.driver.find_elements(By.CSS_SELECTOR, "#images .thumb")

    def jobs(self) -> list[WebElement]:
        return self.driver.find_elements(By.CSS_SELECTOR, "#queue .job")

    def fetch_status(self, path: str) -> int:
        return int(self.driver.execute_async_script("fetch(arguments[0]).then(a => arguments[1](a.status))", path))


def page_for(browser: WebDriver, service: Service) -> Page:
    return Page(browser, f"http://127.0.0.1:{service.port}/")


def token_for(service: Service, scope: str) -> str:
    return service.mia("token", "create", "--for", f"{scope}-{time.monotonic_ns()}", "--scope", scope).stdout.strip()


def made(service: Service, prompt: str = "a fox", **settings: Any) -> dict[str, Any]:
    job = service.finished(service.submit({"prompt": prompt, **PIXEL, "seed": 7, **settings}))
    assert job["state"] == "done"
    return job


def test_signing_in_leaves_no_token_in_the_browser_and_lasts(browser: WebDriver, service: Service) -> None:
    page = page_for(browser, service)
    page.sign_in(service.token)
    assert service.token not in json.dumps(page.run("return [document.cookie, {...localStorage}, {...sessionStorage}, document.body.innerHTML]"))
    assert page.find("token").get_attribute("value") == ""
    assert page.run("return document.cookie") == ""  # a script on the page can't read the session
    browser.refresh()
    page.wait(lambda: page.shown("main-view"))
    assert page.find("device").text.startswith("smoke-")


def test_a_wrong_token_is_refused_with_a_reason(browser: WebDriver, service: Service) -> None:
    page = page_for(browser, service)
    page.wait(lambda: page.shown("sign-in-view"))
    page.type("token", "mia_" + "x" * 43)
    page.click_submit("sign-in-form")
    page.wait(lambda: "wasn't accepted" in page.find("sign-in-problem").text)
    assert not page.shown("main-view")


def test_asking_while_signed_out_never_locks_you_out(browser: WebDriver, service: Service) -> None:
    # The page asks once if it's signed in, and Safari asks for icons by itself. None of that counts as a refusal.
    page = page_for(browser, service)
    for _ in range(35):
        assert page.fetch_status("/favicon.ico") == 401
    page.sign_in(service.token)


def test_the_form_makes_a_picture_that_reaches_the_gallery(browser: WebDriver, service: Service) -> None:
    page = page_for(browser, service)
    page.sign_in(service.token)
    page.wait(lambda: page.find("model").get_attribute("value"))
    page.wait(lambda: "No pictures yet" in page.find("images-empty").text)
    page.type("prompt", "a red fox")
    Select(page.find("size")).select_by_value("512x512")
    page.click_submit("generate")
    page.wait(page.thumbnails)
    picture = page.thumbnails()[0].find_element(By.TAG_NAME, "img")
    page.wait(lambda: browser.execute_script("return arguments[0].naturalWidth", picture) > 0)  # loaded, with only the cookie
    assert picture.get_attribute("alt") == "a red fox"
    assert not page.shown("images-empty")


def test_a_custom_size_and_settings_reach_the_job(browser: WebDriver, service: Service) -> None:
    page = page_for(browser, service)
    page.sign_in(service.token)
    page.wait(lambda: page.find("model").get_attribute("value"))
    page.type("prompt", "a custom job")
    Select(page.find("size")).select_by_value("custom")
    assert page.shown("custom-size")
    page.type("width", "640")
    page.type("height", "384")
    page.type("seed", "99")
    page.type("steps", "3")
    page.click_submit("generate")
    page.wait(page.thumbnails)
    _status, data = service.request("GET", "/v1/images")
    image = json.loads(data)["images"][0]
    assert (image["width"], image["height"], image["seed"], image["steps"]) == (640, 384, 99, 3)


def test_a_refused_job_says_why(browser: WebDriver, service: Service) -> None:
    page = page_for(browser, service)
    page.sign_in(service.token)
    page.wait(lambda: page.find("model").get_attribute("value"))
    page.type("prompt", "   ")  # the browser's own checks let this through, and mia's don't
    page.click_submit("generate")
    page.wait(lambda: "the prompt is empty" in page.find("generate-problem").text)
    assert page.find("generate-button").is_enabled()


def test_progress_is_live_and_a_job_can_be_stopped(browser: WebDriver, service: Service) -> None:
    page = page_for(browser, service)
    page.sign_in(service.token)
    job = service.submit({"prompt": "a long job", "width": 512, "height": 512, "steps": 50, "seed": 1})
    browser.refresh()
    page.wait(lambda: browser.find_elements(By.CSS_SELECTOR, f'[data-job="{job}"] .tile.lit'))
    # Faster than the page polls, so this can only be the event stream.
    first = len(browser.find_elements(By.CSS_SELECTOR, ".tile.lit"))
    page.wait(lambda: len(browser.find_elements(By.CSS_SELECTOR, ".tile.lit")) >= first + 2, seconds=3)
    assert "Step" in page.jobs()[0].text
    page.jobs()[0].find_element(By.TAG_NAME, "button").click()
    assert service.finished(job)["state"] == "cancelled"
    page.wait(lambda: page.shown("queue-empty"), seconds=10)


def test_a_failed_job_shows_its_reason(browser: WebDriver, service: Service) -> None:
    job = service.submit({"prompt": "a doomed job", "width": 512, "height": 512, "steps": 50, "seed": 1})
    service.running(job)
    os.kill(service.worker_pid(), 9)
    assert service.finished(job)["state"] == "failed"
    page = page_for(browser, service)
    page.sign_in(service.token)
    page.wait(lambda: page.jobs() and "Failed" in page.jobs()[0].text)
    assert browser.find_element(By.CSS_SELECTOR, ".job-error").text


def test_text_from_a_prompt_stays_text(browser: WebDriver, service: Service) -> None:
    made(service, HOSTILE)
    running = service.submit({"prompt": HOSTILE, "width": 512, "height": 512, "steps": 50, "seed": 2})
    service.running(running)
    page = page_for(browser, service)
    page.sign_in(service.token)
    page.wait(lambda: page.thumbnails() and page.jobs())
    page.thumbnails()[0].click()
    page.wait(lambda: page.shown("detail"))
    assert page.find("detail-prompt").text == HOSTILE
    assert page.jobs()[0].find_element(By.CSS_SELECTOR, ".job-prompt").get_attribute("textContent") == HOSTILE
    assert page.run('return document.querySelectorAll("img[src=x], #queue script, #detail script").length') == 0
    assert page.run("return window.pwned") is None


def test_the_page_runs_no_script_of_someone_elses(browser: WebDriver, service: Service) -> None:
    page = page_for(browser, service)
    page.wait(lambda: page.shown("sign-in-view"))
    page.run('const s = document.createElement("script"); s.textContent = "window.inlined = 1"; document.head.append(s)')
    page.run('const i = document.createElement("img"); i.src = "x"; i.setAttribute("onerror", "window.inlined = 2"); document.body.append(i)')
    time.sleep(0.3)
    assert page.run("return window.inlined") is None


def test_an_image_reopens_with_its_settings(browser: WebDriver, service: Service) -> None:
    made(service, "reopen me", seed=1234, steps=3)
    page = page_for(browser, service)
    page.sign_in(service.token)
    page.wait(page.thumbnails)
    page.wait(lambda: page.find("model").get_attribute("value"))
    page.thumbnails()[0].click()
    page.wait(lambda: page.shown("detail"))
    assert page.find("detail-prompt").text == "reopen me"
    settings = page.find("detail-settings").text
    assert "1234" in settings and "512 \N{MULTIPLICATION SIGN} 512" in settings
    page.wait(lambda: browser.execute_script("return arguments[0].naturalWidth", page.find("detail-image")) > 0)
    assert page.fetch_status(page.find("detail-file").get_attribute("href") or "") == 200
    page.click("detail-reuse")
    page.wait(lambda: not page.shown("detail"))
    assert page.find("prompt").get_attribute("value") == "reopen me"
    assert page.find("seed").get_attribute("value") == "1234"
    assert page.find("steps").get_attribute("value") == "3"
    assert Select(page.find("size")).first_selected_option.get_attribute("value") == "512x512"


def test_search_and_more(browser: WebDriver, service: Service) -> None:
    for number in range(3):
        made(service, f"fox {number}", seed=number)
    made(service, "a whale", seed=9)
    page = page_for(browser, service)
    page.sign_in(service.token)
    page.wait(lambda: len(page.thumbnails()) == 4)
    assert not page.shown("more")  # fewer than a page
    page.type("search", "whale")
    page.wait(lambda: len(page.thumbnails()) == 1)
    page.type("search", "nothing like this")
    page.wait(lambda: "No pictures match" in page.find("images-empty").text)


def test_read_scope_can_look_but_not_make_or_delete(browser: WebDriver, service: Service) -> None:
    made(service)
    page = page_for(browser, service)
    page.sign_in(token_for(service, "read"))
    page.wait(page.thumbnails)
    assert page.shown("read-only") and not page.shown("generate")
    page.thumbnails()[0].click()
    page.wait(lambda: page.shown("detail"))
    assert not page.shown("detail-delete") and not page.shown("detail-reuse")


def test_generate_scope_cant_delete_but_admin_can_after_being_sure(browser: WebDriver, service: Service) -> None:
    job = made(service)
    page = page_for(browser, service)
    page.sign_in(service.token)
    page.wait(page.thumbnails)
    page.thumbnails()[0].click()
    page.wait(lambda: page.shown("detail"))
    assert not page.shown("detail-delete")
    page.click("sign-out")
    page.sign_in(token_for(service, "admin"))
    page.wait(page.thumbnails)
    page.thumbnails()[0].click()
    page.wait(lambda: page.shown("detail"))
    page.click("detail-delete")
    assert page.find("detail-delete").text == "Delete for good?"
    assert service.request("GET", job["links"]["image"])[0] == 200  # one press does nothing
    page.click("detail-delete")
    page.wait(lambda: not page.thumbnails())
    assert service.request("GET", job["links"]["image"])[0] == 404


def test_signing_out_clears_the_page_and_the_session(browser: WebDriver, service: Service) -> None:
    made(service)
    page = page_for(browser, service)
    page.sign_in(service.token)
    page.wait(page.thumbnails)
    page.click("sign-out")
    page.wait(lambda: page.shown("sign-in-view"))
    assert not page.thumbnails()
    assert page.fetch_status("/v1/session") == 401
    assert browser.get_cookies() == []


def test_revoking_the_token_signs_the_page_out(browser: WebDriver, service: Service) -> None:
    page = page_for(browser, service)
    name = f"revoke-{time.monotonic_ns()}"
    token = service.mia("token", "create", "--for", name).stdout.strip()
    page.sign_in(token)
    service.mia("token", "revoke", name)
    page.click("search")
    page.type("search", "x")
    page.wait(lambda: page.shown("sign-in-view"))
    assert "ended" in page.find("sign-in-problem").text


class _Attacker(http.server.BaseHTTPRequestHandler):
    """Another program's web page on this Mac. It counts as the same site as mia, because a site ignores ports."""

    target = ""
    page = """<!doctype html><title>attack</title><script>
    const url = "TARGET";
    const job = JSON.stringify({prompt: "attacker", width: 512, height: 512, steps: 2});
    Promise.allSettled([
      fetch(url + "/v1/jobs", {method: "POST", mode: "no-cors", credentials: "include", headers: {"Content-Type": "text/plain"}, body: job}),
      fetch(url + "/v1/jobs", {method: "POST", credentials: "include", headers: {"Content-Type": "application/json"}, body: job}),
      fetch(url + "/v1/session", {method: "DELETE", credentials: "include"}),
      fetch(url + "/v1/jobs", {credentials: "include"}).then(a => a.text()).then(t => { window.stolen = t; }),
    ]).then(() => { document.title = "finished"; });
    </script>"""

    def log_message(self, *_args: object) -> None:
        pass

    def do_GET(self) -> None:
        body = self.page.replace("TARGET", self.target).encode()
        self.send_response(200 if self.path == "/" else 404)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def test_another_programs_page_on_this_mac_cant_use_the_cookie(browser: WebDriver, service: Service) -> None:
    page = page_for(browser, service)
    page.sign_in(service.token)
    _Attacker.target = f"http://127.0.0.1:{service.port}"
    attacker = http.server.ThreadingHTTPServer(("127.0.0.1", free_port()), _Attacker)
    threading.Thread(target=attacker.serve_forever, daemon=True).start()
    try:
        browser.get(f"http://127.0.0.1:{attacker.server_address[1]}/")
        WebDriverWait(browser, 15).until(lambda driver: driver.title == "finished")
        assert browser.execute_script("return window.stolen") is None
    finally:
        attacker.shutdown()
        attacker.server_close()
    _status, data = service.request("GET", "/v1/jobs")
    assert json.loads(data)["jobs"] == []
    browser.get(f"http://127.0.0.1:{service.port}/")  # and the attempt to sign out did nothing
    page.wait(lambda: page.shown("main-view"))


def test_a_phone_width_doesnt_scroll_sideways(browser: WebDriver, service: Service) -> None:
    made(service, "a very long prompt " * 40)
    page = page_for(browser, service)
    page.sign_in(service.token)
    original = browser.get_window_size()
    try:
        browser.set_window_size(390, 844)
    except WebDriverException:
        pytest.skip("this browser won't resize")
    try:
        page.wait(page.thumbnails)
        width = page.run("return window.innerWidth")
        if width > 500:
            pytest.skip("this browser's window won't go that narrow")
        assert page.run("return document.documentElement.scrollWidth") <= width
        # Scrolled first, as a person would. Asked to do both, Safari's driver scrolled to a picture partly off the window, and
        # its click never reached it.
        browser.execute_script('arguments[0].scrollIntoView({block: "center"})', page.thumbnails()[0])
        page.thumbnails()[0].click()
        page.wait(lambda: page.shown("detail"))
        assert page.run("return document.documentElement.scrollWidth") <= width
        assert page.run('return document.getElementById("detail").getBoundingClientRect().width') <= width
    finally:
        browser.set_window_size(original["width"], original["height"])


def test_over_https_the_cookie_is_secure_and_everything_works(browser: WebDriver, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    if browser.capabilities["browserName"].lower() == "safari":
        pytest.skip("Safari won't accept mia's authority unless the Mac trusts it")
    folder, https_port = tmp_path / "secure", free_port()
    monkeypatch.setenv("MIA_HOME", str(folder))
    settings = remote.Settings(("localhost",), (), https_port)
    paths.make_private(folder)
    paths.make_private(remote.authority_file().parent)
    remote._make_authority(settings.names)
    remote._issue(settings)
    remote._write_private(remote._settings_file(), json.dumps(dataclasses.asdict(settings)).encode())
    running = Service(folder)
    try:
        page = Page(browser, f"https://localhost:{https_port}/")
        page.sign_in(running.token)
        cookie = next(found for found in browser.get_cookies() if found["name"] == "__Host-mia_session")
        assert cookie["secure"] and cookie["httpOnly"] and cookie["sameSite"] == "Strict" and cookie["path"] == "/"
        page.wait(lambda: page.find("model").get_attribute("value"))
        page.type("prompt", "over https")
        Select(page.find("size")).select_by_value("512x512")
        page.click_submit("generate")
        page.wait(page.thumbnails)
        picture = page.thumbnails()[0].find_element(By.TAG_NAME, "img")
        page.wait(lambda: browser.execute_script("return arguments[0].naturalWidth", picture) > 0)
        page.click("sign-out")
        page.wait(lambda: page.shown("sign-in-view"))
        assert browser.get_cookies() == []
    finally:
        running.stop()


def test_diag_where_clicks_land(browser: WebDriver, service: Service) -> None:
    made(service, "short", seed=1)
    made(service, "a very long prompt " * 40, seed=2)
    page = page_for(browser, service)
    page.sign_in(service.token)
    page.wait(lambda: len(page.thumbnails()) == 2)
    page.run("""window.seen = []; window.problems = [];
      for (const kind of ["pointerdown", "click"]) document.addEventListener(kind, (e) => seen.push([kind, Math.round(e.clientX), Math.round(e.clientY), e.target.tagName]), true);
      window.addEventListener("error", (e) => problems.push(e.message));""")
    rect = "const r = arguments[0].getBoundingClientRect(); return [r.x, r.y, r.width, r.height].map(Math.round)"
    lines = []
    original = browser.get_window_size()
    for size, how in (("wide", "driver"), ("narrow", "driver"), ("narrow", "script")):
        if size == "narrow":
            browser.set_window_size(390, 844)
            time.sleep(1)
        for index, label in ((1, "short"), (0, "long")):
            thumb = page.thumbnails()[index]
            browser.execute_script('arguments[0].scrollIntoView({block: "center"})', thumb)
            time.sleep(0.5)
            before = browser.execute_script(rect, thumb)
            page.run("window.seen.length = 0")
            if how == "driver":
                thumb.click()
            else:
                browser.execute_script("arguments[0].click()", thumb)
            time.sleep(1)
            is_open = page.run('return document.getElementById("detail").open')
            lines.append(f"{size} {how} {label}: open={is_open} shown={page.shown('detail')} thumb={before} events={page.run('return window.seen')} window={page.run('return [innerWidth, innerHeight, scrollY, outerWidth, outerHeight]')} problems={page.run('return window.problems')}")
            page.run('document.getElementById("detail").close()')
    browser.set_window_size(original["width"], original["height"])
    pytest.fail("DIAG\n" + "\n".join(lines))
