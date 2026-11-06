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
from selenium.webdriver.common.keys import Keys
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
        chrome.add_argument("--window-size=1280,900")  # it starts at 800 wide otherwise, which gets the narrow layout
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


def test_a_finished_picture_shows_large_with_a_new_variation(browser: WebDriver, service: Service) -> None:
    page = page_for(browser, service)
    page.sign_in(service.token)
    page.wait(lambda: page.find("model").get_attribute("value"))
    assert not page.shown("latest")  # nothing from before you arrived
    page.type("prompt", "a large fox")
    Select(page.find("size")).select_by_value("512x512")
    page.run('document.getElementById("advanced").open = true')
    page.type("seed", "5")
    page.click_submit("generate")
    page.wait(lambda: page.shown("latest"), seconds=30)
    assert page.find("latest-prompt").text == "a large fox"
    page.wait(lambda: browser.execute_script("return arguments[0].naturalWidth", page.find("latest-image")) > 0)
    page.click("latest-variation")
    page.wait(lambda: len(page.thumbnails()) == 2, seconds=30)
    seeds = seeds_made(service)
    assert 5 in seeds and len(set(seeds)) == 2  # the same settings, with a new seed
    _status, data = service.request("GET", "/v1/images")
    assert {(image["prompt"], image["width"]) for image in json.loads(data)["images"]} == {("a large fox", 512)}
    page.click("latest-dismiss")
    assert not page.shown("latest")


def test_opening_the_large_picture_shows_its_settings_and_deleting_it_clears_it(browser: WebDriver, service: Service) -> None:
    page = page_for(browser, service)
    page.sign_in(token_for(service, "admin"))
    page.wait(lambda: page.find("model").get_attribute("value"))
    page.type("prompt", "soon gone")
    Select(page.find("size")).select_by_value("512x512")
    page.click_submit("generate")
    page.wait(lambda: page.shown("latest"), seconds=30)
    page.click("latest-open")
    page.wait(lambda: page.shown("detail"))
    page.click("detail-delete")
    page.click("detail-delete")
    page.wait(lambda: not page.shown("latest"))


def test_a_variation_from_the_picture_view_joins_the_queue(browser: WebDriver, service: Service) -> None:
    made(service, "vary me", seed=3, steps=3)
    page = page_for(browser, service)
    page.sign_in(service.token)
    page.wait(page.thumbnails)
    page.thumbnails()[0].click()
    page.wait(lambda: page.shown("detail"))
    page.click("detail-variation")
    page.wait(lambda: not page.shown("detail"))
    page.wait(lambda: len(page.thumbnails()) == 2, seconds=30)
    _status, data = service.request("GET", "/v1/images")
    images = json.loads(data)["images"]
    assert {image["steps"] for image in images} == {3} and len({image["seed"] for image in images}) == 2


def test_a_tab_in_the_background_says_when_a_picture_is_done(browser: WebDriver, service: Service) -> None:
    page = page_for(browser, service)
    page.sign_in(service.token)
    page.wait(lambda: page.find("model").get_attribute("value"))
    page.run('Object.defineProperty(document, "hidden", {value: true, configurable: true})')
    service.finished(service.submit({"prompt": "while you were away", **PIXEL, "seed": 1}))
    page.run('document.getElementById("search").value = ""; poll()')
    page.wait(lambda: browser.title == "(1) mia")
    page.run('Object.defineProperty(document, "hidden", {value: false, configurable: true}); document.dispatchEvent(new Event("visibilitychange"))')
    page.wait(lambda: browser.title == "mia")


def test_the_form_remembers_your_choices_but_never_the_prompt(browser: WebDriver, service: Service) -> None:
    page = page_for(browser, service)
    page.sign_in(service.token)
    page.wait(lambda: page.find("model").get_attribute("value"))
    assert Select(page.find("size")).first_selected_option.get_attribute("value") == "1024x1024"
    page.type("prompt", "a secret prompt")
    Select(page.find("size")).select_by_value("1344x768")
    Select(page.find("count")).select_by_value("2")
    page.click_submit("generate")
    page.wait(lambda: len(page.jobs()) + len(page.thumbnails()) >= 1)
    page.wait(lambda: page.run('return localStorage.getItem("mia.form")'))
    stored = page.run("return JSON.stringify({...localStorage})")
    assert "secret" not in stored and service.token not in stored
    browser.refresh()
    page.wait(lambda: page.shown("main-view"))
    page.wait(lambda: page.find("model").get_attribute("value"))
    page.wait(lambda: Select(page.find("size")).first_selected_option.get_attribute("value") == "1344x768")
    assert Select(page.find("count")).first_selected_option.get_attribute("value") == "2"
    assert page.find("prompt").get_attribute("value") == ""


@pytest.mark.parametrize("stored", ["not json", '{"size": "9x9", "model": "gone", "count": "999"}', "null"])
def test_a_damaged_memory_of_the_form_is_ignored(browser: WebDriver, service: Service, stored: str) -> None:
    page = page_for(browser, service)
    browser.execute_script('localStorage.setItem("mia.form", arguments[0])', stored)
    browser.refresh()
    page.sign_in(service.token)
    page.wait(lambda: page.find("model").get_attribute("value"))
    assert Select(page.find("size")).first_selected_option.get_attribute("value") == "1024x1024"
    assert Select(page.find("count")).first_selected_option.get_attribute("value") == "1"


def test_on_a_wide_screen_the_gallery_sits_at_the_top_until_a_picture_finishes(browser: WebDriver, service: Service) -> None:
    def top(element_id: str) -> float:
        return float(browser.execute_script("return arguments[0].getBoundingClientRect().top", page.find(element_id)))

    page = page_for(browser, service)
    page.sign_in(service.token)
    page.wait(lambda: page.find("model").get_attribute("value"))
    assert abs(top("gallery-title") - top("compose")) < 30
    page.type("prompt", "a fox")
    Select(page.find("size")).select_by_value("512x512")
    page.click_submit("generate")
    page.wait(lambda: page.shown("latest"), seconds=30)
    assert abs(top("latest") - top("compose")) < 30
    assert top("gallery-title") > top("latest") + 100
    page.click("latest-dismiss")
    assert abs(top("gallery-title") - top("compose")) < 30


def open_first_picture(page: Page) -> None:
    page.wait(page.thumbnails)
    page.driver.execute_script("arguments[0].click()", page.thumbnails()[0])
    page.wait(lambda: page.shown("detail"))


def test_moving_between_pictures_with_buttons_and_keys(browser: WebDriver, service: Service) -> None:
    for name in ("oldest", "middle", "newest"):
        made(service, name)
    page = page_for(browser, service)
    page.sign_in(service.token)
    page.wait(lambda: len(page.thumbnails()) == 3)
    open_first_picture(page)
    assert page.find("detail-prompt").text == "newest"
    assert not page.find("detail-previous").is_enabled()
    page.click("detail-next")
    page.wait(lambda: page.find("detail-prompt").text == "middle")
    page.find("detail").send_keys(Keys.ARROW_RIGHT)
    page.wait(lambda: page.find("detail-prompt").text == "oldest")
    assert not page.find("detail-next").is_enabled()
    page.find("detail").send_keys(Keys.ARROW_RIGHT)  # the end: it stays
    assert page.find("detail-prompt").text == "oldest"
    page.find("detail").send_keys(Keys.ARROW_LEFT)
    page.wait(lambda: page.find("detail-prompt").text == "middle")
    page.click("detail-previous")
    page.wait(lambda: page.find("detail-prompt").text == "newest")
    assert page.shown("detail")


def test_next_loads_more_at_the_end_of_the_page(browser: WebDriver, service: Service) -> None:
    for number in range(25):
        made(service, f"picture {number}", seed=number, steps=1)
    page = page_for(browser, service)
    page.sign_in(service.token)
    page.wait(lambda: len(page.thumbnails()) == 24)
    assert page.shown("more")
    browser.execute_script("arguments[0].click()", page.thumbnails()[-1])
    page.wait(lambda: page.shown("detail"))
    assert page.find("detail-prompt").text == "picture 1"
    assert page.find("detail-next").is_enabled()
    page.click("detail-next")
    page.wait(lambda: page.find("detail-prompt").text == "picture 0")
    assert len(page.thumbnails()) == 25 and not page.shown("more")
    assert not page.find("detail-next").is_enabled()


def test_a_picture_can_be_downloaded_and_its_prompt_copied(browser: WebDriver, service: Service) -> None:
    job = made(service, "copy this")
    page = page_for(browser, service)
    page.sign_in(service.token)
    open_first_picture(page)
    link = page.find("detail-download")
    assert link.get_attribute("download") == f"mia-{job['image']}.png"
    assert page.fetch_status(link.get_attribute("href") or "") == 200
    # A stand-in, as a browser may refuse a real clipboard to a test, and these tests shouldn't touch the Mac's own.
    page.run('window.copied = []; Object.defineProperty(navigator, "clipboard", {configurable: true, value: {writeText: async (text) => { copied.push(text); }}})')
    page.click("detail-copy")
    page.wait(lambda: page.find("detail-copy").text == "Copied")
    assert page.run("return window.copied") == ["copy this"]
    page.run('Object.defineProperty(navigator, "clipboard", {value: {writeText: async () => { throw new Error("no"); }}})')
    page.click("detail-copy")
    page.wait(lambda: "wouldn't copy" in page.find("detail-problem").text)


def test_command_or_control_enter_generates(browser: WebDriver, service: Service) -> None:
    page = page_for(browser, service)
    page.sign_in(service.token)
    page.wait(lambda: page.find("model").get_attribute("value"))
    Select(page.find("size")).select_by_value("512x512")
    page.type("prompt", "from the keyboard")
    page.find("prompt").send_keys(Keys.CONTROL, Keys.ENTER)
    page.wait(page.thumbnails, seconds=30)
    assert page.find("prompt").get_attribute("value") == "from the keyboard"  # and no new line was added


def test_the_status_line_says_how_things_stand(browser: WebDriver, service: Service) -> None:
    page = page_for(browser, service)
    page.sign_in(service.token)
    page.wait(lambda: "Worker running" in page.find("status").text)
    assert "GB free" in page.find("status").text
    page.run('showStatus({worker: {running: false}, disk_free_bytes: 1024})')
    text = page.find("status").text
    assert "isn't running" in text and "Nearly out of disk" in text
    assert "problem" in (page.find("status").get_attribute("class") or "")


def seeds_made(service: Service) -> list[int]:
    _status, data = service.request("GET", "/v1/images")
    return sorted(image["seed"] for image in json.loads(data)["images"])


def test_several_pictures_each_get_their_own_seed(browser: WebDriver, service: Service) -> None:
    page = page_for(browser, service)
    page.sign_in(service.token)
    page.wait(lambda: page.find("model").get_attribute("value"))
    page.type("prompt", "a set")
    Select(page.find("size")).select_by_value("512x512")
    Select(page.find("count")).select_by_value("3")
    page.click_submit("generate")
    page.wait(lambda: len(page.thumbnails()) == 3, seconds=40)
    assert len(set(seeds_made(service))) == 3


def test_several_pictures_count_up_from_a_seed_and_wrap_at_the_end(browser: WebDriver, service: Service) -> None:
    page = page_for(browser, service)
    page.sign_in(service.token)
    page.wait(lambda: page.find("model").get_attribute("value"))
    page.type("prompt", "a set")
    Select(page.find("size")).select_by_value("512x512")
    Select(page.find("count")).select_by_value("2")
    page.run('document.getElementById("advanced").open = true')
    page.type("seed", "4294967295")
    page.click_submit("generate")
    page.wait(lambda: len(page.thumbnails()) == 2, seconds=40)
    assert seeds_made(service) == [0, 4294967295]


def test_a_queue_filling_partway_says_how_many_were_added(browser: WebDriver, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Slow enough that the running job can't finish and free a place while this runs, on a slow machine.
    monkeypatch.setattr("test_smoke.STEP_SECONDS", "5")
    service = Service(tmp_path / "mia")
    try:
        long = {"prompt": "waiting", "width": 512, "height": 512, "steps": 50}
        service.running(service.submit({**long, "seed": 0}))
        for seed in range(1, 19):  # one running, and 18 of the 20 places taken
            service.submit({**long, "seed": seed})

        def asked_for() -> int:
            _status, data = service.request("GET", "/v1/jobs?limit=50")
            return sum(job["prompt"] == "too many" for job in json.loads(data)["jobs"])

        page = page_for(browser, service)
        page.sign_in(service.token)
        page.wait(lambda: page.find("model").get_attribute("value"))
        page.type("prompt", "too many")
        Select(page.find("count")).select_by_value("4")
        page.click_submit("generate")
        page.wait(lambda: "2 of 4 pictures were added" in page.find("generate-problem").text)
        assert asked_for() == 2
        # Sending it again finds the two it already made, so nothing is made twice.
        page.run('document.getElementById("generate-problem").textContent = ""')
        page.click_submit("generate")
        page.wait(lambda: "2 of 4 pictures were added" in page.find("generate-problem").text)
        assert asked_for() == 2
    finally:
        service.stop()


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
    Select(page.find("count")).select_by_value("4")  # reusing a picture makes that one picture
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
    assert Select(page.find("count")).first_selected_option.get_attribute("value") == "1"


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
        page.click("view-gallery")
        width = page.run("return window.innerWidth")
        if width > 500:
            pytest.skip("this browser's window won't go that narrow")
        assert page.run("return document.documentElement.scrollWidth") <= width
        # From a script, because this test is about layout: once its window has been resized, none of the clicks from Safari's
        # driver reached the page, not even as a pointerdown. Other tests click for real, before anything resizes the window.
        browser.execute_script("arguments[0].click()", page.thumbnails()[0])
        page.wait(lambda: page.shown("detail"))
        assert page.run("return document.documentElement.scrollWidth") <= width
        assert page.run('return document.getElementById("detail").getBoundingClientRect().width') <= width
    finally:
        browser.set_window_size(original["width"], original["height"])


def test_a_narrow_window_switches_between_making_and_browsing(browser: WebDriver, service: Service) -> None:
    # Clicks from a script, for the reason in the test above.
    made(service, "a fox")
    page = page_for(browser, service)
    page.sign_in(service.token)
    original = browser.get_window_size()
    browser.set_window_size(390, 844)
    try:
        page.wait(page.thumbnails)
        if page.run("return window.innerWidth") > 500:
            pytest.skip("this browser's window won't go that narrow")
        assert page.shown("view-make") and page.shown("generate") and not page.thumbnails()[0].is_displayed()
        page.click("view-gallery")
        page.wait(page.thumbnails()[0].is_displayed)
        assert not page.shown("generate")
        page.click("view-make")
        page.wait(lambda: page.shown("generate"))
        short = page.run("""return [...document.querySelectorAll("button, select, input, summary")]
            .filter((e) => e.getClientRects().length && e.getBoundingClientRect().height < 44)
            .map((e) => e.id || e.className || e.tagName)""")
        assert short == []  # big enough for a finger
        job = service.submit({"prompt": "a long job", "width": 512, "height": 512, "steps": 50, "seed": 1})
        page.click("view-gallery")
        browser.refresh()
        page.wait(lambda: page.find("view-make").text == "Make (1)")
        assert service.request("DELETE", f"/v1/jobs/{job}")[0] == 202
    finally:
        browser.set_window_size(original["width"], original["height"])


def test_read_scope_starts_on_the_gallery_in_a_narrow_window(browser: WebDriver, service: Service) -> None:
    page = page_for(browser, service)
    page.sign_in(token_for(service, "read"))
    assert page.find("main-view").get_attribute("data-view") == "gallery"


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
