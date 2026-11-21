"""
IHK-Termin-Monitor
Prüft die IHK-Terminseite und schickt dir eine Push-Nachricht (ntfy),
sobald eine Gesamtprüfung (keine Wiederholungsprüfung) an einem dieser Tage
NICHT mehr "Ausgebucht" ist (egal in welcher Stadt):
  20.01.2027, 21.01.2027, 17.02.2027, 18.02.2027, 18.03.2027
"""
import json
import os
import re
import sys
import time
import urllib.request

# ---------------- Einstellungen ----------------
BASE_URL = "https://ihkportal.de/nordwestfalen/tibrosPP/participant/exam/SKBW25?tab=registerToExam&pageNumber={page}"
MAX_PAGES = 10
GEWUENSCHTE_TITEL = {"gesamtprüfung"}              # Wiederholungsprüfungen zählen NICHT
GEWUENSCHTE_DATEN = {              # nur Gesamtprüfungen an diesen Tagen
    "20.01.2027",
    "21.01.2027",
    "17.02.2027",
    "18.02.2027",
    "18.03.2027",
}
LAUFZEIT_SEKUNDEN = 340 * 60      # ein Lauf dauert ca. 5 h 40 min (GitHub-Limit: 6 h)
PRUEF_INTERVALL = 300             # alle 5 Minuten prüfen (nicht schneller, sonst droht eine Sperre)
ERINNERUNG_SEKUNDEN = 15 * 60     # solange ein Termin frei ist, alle 15 Minuten erinnern
FEHLER_BIS_WARNUNG = 6            # nach 6 Fehlversuchen in Folge (30 Min) kommt eine Warn-Push
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
        and b["datum"] in GEWUENSCHTE_DATEN
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

    start = time.time()
    erfolgreich = 0
    fehler_in_folge = 0
    letzte_meldung = {}   # (ort, datum) -> Zeitpunkt der letzten Push-Nachricht
    fehlende_gemeldet = set()
    durchgang = 0

    while True:
        durchgang += 1
        t0 = time.time()
        stempel = time.strftime("%H:%M:%S", time.gmtime())
        try:
            blocks = hole_alle_blocks()
        except Exception as e:
            print(f"[{stempel} UTC] #{durchgang} Fehler beim Laden: {e}", flush=True)
            blocks = []

        if blocks:
            erfolgreich += 1
            if fehler_in_folge >= FEHLER_BIS_WARNUNG:
                push("IHK-Monitor läuft wieder", "Die Terminseite ist wieder lesbar.", prio=3, click=link)
            fehler_in_folge = 0

            beobachtet = [
                b for b in blocks
                if b["titel"].lower() in GEWUENSCHTE_TITEL and b["datum"] in GEWUENSCHTE_DATEN
            ]
            zusammenfassung = ", ".join(
                f"{b['ort']} {b['datum']} {'ausgebucht' if b['ausgebucht'] else 'FREI'}" for b in beobachtet
            )
            print(f"[{stempel} UTC] #{durchgang} {len(blocks)} Termine gelesen. Beobachtet: {zusammenfassung}", flush=True)

            gefunden = {b["datum"] for b in beobachtet}
            for datum in sorted(GEWUENSCHTE_DATEN - gefunden - fehlende_gemeldet):
                fehlende_gemeldet.add(datum)
                print(f"WARNUNG: Keine Gesamtprüfung am {datum} auf der Seite gefunden.", flush=True)

            freie = finde_freie(blocks)
            freie_keys = {(b["ort"], b["datum"]) for b in freie}
            # Termine, die wieder ausgebucht sind, zurücksetzen
            for key in list(letzte_meldung):
                if key not in freie_keys:
                    del letzte_meldung[key]

            for b in freie:
                key = (b["ort"], b["datum"])
                if key in letzte_meldung and time.time() - letzte_meldung[key] < ERINNERUNG_SEKUNDEN:
                    continue
                letzte_meldung[key] = time.time()
                push(
                    f"Termin frei: {b['ort']} {b['datum']}",
                    f"{b['titel']} in {b['ort']} am {b['datum']} ist nicht mehr ausgebucht! Schnell anmelden.\n\n{b['text']}",
                    click=link,
                )
                print("Benachrichtigung gesendet für", b["ort"], b["datum"], flush=True)
        else:
            fehler_in_folge += 1
            print(f"[{stempel} UTC] #{durchgang} Keine Termine erkannt ({fehler_in_folge} in Folge).", flush=True)
            if fehler_in_folge == FEHLER_BIS_WARNUNG:
                push(
                    "IHK-Monitor: Problem",
                    "Seit ca. 30 Minuten ist die Terminseite nicht lesbar. Eventuell hat die IHK die Seite umgebaut.",
                    prio=3, click=link,
                )

        if time.time() - start + PRUEF_INTERVALL > LAUFZEIT_SEKUNDEN:
            break
        time.sleep(max(1, PRUEF_INTERVALL - (time.time() - t0)))

    if erfolgreich == 0:
        # In diesem Lauf nie lesbar -> Lauf als fehlgeschlagen markieren (GitHub schickt dir dann eine Mail)
        print("FEHLER: In diesem Lauf konnte keine Prüfung erfolgreich durchgeführt werden.")
        sys.exit(1)


if __name__ == "__main__":
    main()
