"""О2.4: повторні спроби в клієнті сайту — перевірка в реальному браузері (Playwright + Microsoft Edge).

Сценарій A «втрачена відповідь»: вмикається відмова after_commit_before_response; пацієнт натискає «1 слот».
Екземпляр, що обробив запит, фіксує запис і падає — браузер не отримує відповіді. Клієнт повторює запит
із тим самим Idempotency-Key і показує 201 зі збереженим результатом; у базі — один запис.

Сценарій B «результат невідомий»: app-a і app-b зупинено; клієнт вичерпує спроби/час і показує
«Результат невідомий». Після запуску екземплярів кнопка «Перевірити ще раз (той самий ключ)» дає 201.

Потрібно: стек запущено, клієнт на :8081 (python -m http.server 8081 --directory web/client),
pip install playwright. Із кореня проєкту:  python web/experiments/lab2/client_retry.py
"""

import argparse
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).parent))
import lab2common as c  # noqa: E402

SITE = "http://localhost:8081/"


def login(page, login_name, password):
    page.goto(SITE)
    page.wait_for_selector("#login-form")
    page.fill("#login-form input[name=login]", login_name)
    page.fill("#login-form input[name=password]", password)
    page.click("#login-form button.primary")
    page.wait_for_selector("#me-view:not([hidden])")
    page.click(".doctor:has-text('Іваненко')")
    page.wait_for_selector("button[data-day='dd-doc1-1203']")


def main() -> int:
    c.utf8_stdout()
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="web/results/lab2/client-retry")
    a = ap.parse_args()
    out = c.ROOT / a.out
    out.mkdir(parents=True, exist_ok=True)
    c.wait_ready()
    c.psql("DELETE FROM booking WHERE channel = 'web' AND patient_id IN ('p-1003', 'p-1005') AND doctor_day_id = 'dd-doc1-1203';"
           "UPDATE doctor_day d SET slots_taken = (SELECT coalesce(sum(slots),0) FROM booking b WHERE b.doctor_day_id = d.id "
           "AND b.status IN ('held','confirmed','visited','no_show')) WHERE id = 'dd-doc1-1203';")
    summary = {}
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="msedge", headless=True)

        # --- A: втрачена відповідь
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        login(page, "patient3", "patient-pass")
        c.compose("exec", "-T", "app-a", "touch", "/faults/after_commit_before_response")
        page.click("button[data-day='dd-doc1-1203'][data-slots='1']")
        page.wait_for_selector("#book-result .ok-box", timeout=40_000)
        page.wait_for_timeout(400)
        page.screenshot(path=str(out / "a_lost_response.png"))
        text_a = page.inner_text("#book-result")
        count_a = int(c.psql("SELECT count(*) FROM booking WHERE patient_id = 'p-1003' AND doctor_day_id = 'dd-doc1-1203'"))
        summary["lost_response"] = {"client_message": text_a, "bookings_in_db": count_a}
        page.close()
        c.wait_ready()

        # --- B: результат невідомий
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        login(page, "patient5", "patient-pass")
        c.compose("stop", "app-a", "app-b")
        page.click("button[data-day='dd-doc1-1203'][data-slots='1']")
        page.wait_for_selector("#retry-same-key", timeout=40_000)
        page.wait_for_timeout(300)
        page.screenshot(path=str(out / "b_unknown.png"))
        text_b = page.inner_text("#book-result")
        count_before = int(c.psql("SELECT count(*) FROM booking WHERE patient_id = 'p-1005' AND doctor_day_id = 'dd-doc1-1203'"))
        c.compose("start", "app-a", "app-b")
        c.wait_ready()
        page.click("#retry-same-key")
        page.wait_for_selector("#book-result .ok-box", timeout=40_000)
        page.wait_for_timeout(300)
        page.screenshot(path=str(out / "b_after_retry.png"))
        text_b2 = page.inner_text("#book-result")
        count_b = int(c.psql("SELECT count(*) FROM booking WHERE patient_id = 'p-1005' AND doctor_day_id = 'dd-doc1-1203'"))
        summary["unknown_outcome"] = {"client_message": text_b, "bookings_while_down": count_before,
                                      "after_retry_message": text_b2, "bookings_after_retry": count_b}
        browser.close()

    ok = (count_a == 1 and "Спроба" in text_a and "збережений результат" in text_a
          and "Результат невідомий" in text_b and count_before == 0 and count_b == 1)
    summary["ok"] = ok
    c.save(a.out, "summary.json", summary)
    print("A:", text_a.replace("\n", " ")[:400], "| bookings:", count_a)
    print("B:", text_b.replace("\n", " ")[:300], "| bookings:", count_before, "->", count_b)
    print("OK" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
