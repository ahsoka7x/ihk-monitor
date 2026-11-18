"""
IHK-Termin-Monitor
Prüft die IHK-Terminseite und schickt dir eine Push-Nachricht (ntfy),
sobald eine Gesamtprüfung in Münster oder Gelsenkirchen NICHT mehr "Ausgebucht" ist.
"""
import json
import os
import re
import sys
import urllib.request

# ---------------- Einstellungen ----------------
BASE_URL = "https://ihkportal.de/nordwestfalen/tibrosPP/participant/exam/SKBW25?tab=registerToExam&pageNumber={page}"
MAX_PAGES = 10
GEWUENSCHTE_TITEL = {"gesamtprüfung"}              # Wiederholungsprüfungen zählen NICHT
GEWUENSCHTE_ORTE = {"münster", "gelsenkirchen"}
# -----------------------------------------------

DATE_RE = re.compile(r"^\d{2}\.\d{2}\.\d{4}$")
DAY_RE = re.compile(r"^\d{1,2}$")
MONTH_RE = re.compile(r"^[A-Za-zÄÖÜäöü]{3,4}\.?$")


def parse_blocks(text):
    """Zerlegt den sichtbaren Seitentext in einzelne Termin-Karten."""
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    starts = [
        i for i in range(len(lines) - 1)
        if DAY_RE.match(lines[i]) and MONTH_RE.match(lines[i + 1])
    ]
    blocks = []
    for n, s in enumerate(starts):
        end = starts[n + 1] if n + 1 < len(starts) else len(lines)
        chunk = lines[s + 2:end]
        if len(chunk) < 3 or not DATE_RE.match(chunk[2]):
            continue
        blocks.append({
            "titel": chunk[0],
            "ort": chunk[1],
            "datum": chunk[2],
            "text": " | ".join(chunk),
            "ausgebucht": any("ausgebucht" in c.lower() for c in chunk),
        })
    return blocks


def finde_freie(blocks):
    return [
        b for b in blocks
        if b["titel"].lower() in GEWUENSCHTE_TITEL
        and b["ort"].lower() in GEWUENSCHTE_ORTE
        and not b["ausgebucht"]
    ]


def hole_alle_blocks():
    from playwright.sync_api import sync_playwright
    alle, vorherige_signatur = [], None
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(locale="de-DE")
        for nr in range(1, MAX_PAGES + 1):
            page.goto(BASE_URL.format(page=nr), wait_until="networkidle", timeout=60000)
            try:
                page.wait_for_function(
                    "document.body.innerText.includes('Schriftliche')", timeout=20000
                )
            except Exception:
                pass
            page.wait_for_timeout(1500)
            blocks = parse_blocks(page.inner_text("body"))
            if not blocks:
                break
            signatur = blocks[0]["text"] + str(len(blocks))
            if signatur == vorherige_signatur:
                break
            vorherige_signatur = signatur
            alle.extend(blocks)
        browser.close()
    return alle


def push(titel, nachricht, prio=5, click=None):
    topic = os.environ["NTFY_TOPIC"]
    payload = {"topic": topic, "title": titel, "message": nachricht, "priority": prio}
    if click:
        payload["click"] = click
    req = urllib.request.Request(
        "https://ntfy.sh/",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    urllib.request.urlopen(req, timeout=20).read()


def main():
    link = BASE_URL.format(page=1)
    if "--test" in sys.argv:
        push("IHK-Monitor Test", "Wenn du das liest, funktionieren die Push-Nachrichten.", prio=3, click=link)
        print("Test-Nachricht gesendet.")
        return

    blocks = hole_alle_blocks()
    print(f"{len(blocks)} Termine gefunden:")
    for b in blocks:
        print(f"  {b['titel']} | {b['ort']} | {b['datum']} | {'AUSGEBUCHT' if b['ausgebucht'] else 'NICHT ausgebucht'}")

    if not blocks:
        # Seite nicht lesbar -> Lauf als fehlgeschlagen markieren (GitHub schickt dir dann eine Mail)
        print("FEHLER: Keine Termine auf der Seite erkannt.")
        sys.exit(1)

    for b in finde_freie(blocks):
        push(
            f"Termin frei: {b['ort']} {b['datum']}",
            f"{b['titel']} in {b['ort']} am {b['datum']} ist nicht mehr ausgebucht! Schnell anmelden.\n\n{b['text']}",
            click=link,
        )
        print("Benachrichtigung gesendet für", b["ort"], b["datum"])


if __name__ == "__main__":
    main()
