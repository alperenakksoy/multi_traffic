# LaTeX-Report

IEEE-Conference-Format (`IEEEtran`), Quelle ist über `sections/*.tex` aufgeteilt, damit wir Kapitel für Kapitel gemeinsam weiterschreiben können.

## Kompilieren

Empfohlen: **Overleaf** (kostenlos, `IEEEtran.cls` ist dort vorinstalliert).

1. Auf [overleaf.com](https://www.overleaf.com) ein neues, leeres Projekt anlegen.
2. Den gesamten `latex/`-Ordner (oder das `latex_project.zip`, falls vorhanden) hochladen.
3. `main.tex` als Hauptdatei setzen, "Recompile" klicken.

Lokal (falls du eine vollständige TeX-Live-Installation mit `IEEEtran` hast, z.B. via `sudo apt install texlive-publishers` oder `tlmgr install ieeetran`):

```bash
cd latex
pdflatex main.tex
bibtex main
pdflatex main.tex
pdflatex main.tex
```

## Struktur

```
latex/
├── main.tex                  ← Titelseite, Abstract, bindet alle Kapitel ein
├── references.bib
├── figures/
│   ├── hsrw_logo.png
│   └── crl_logo.png
└── sections/
    ├── 01_introduction.tex
    ├── 02_architecture.tex          (fertig, mit TikZ-Diagramm)
    ├── 03_implementation.tex        (fertig)
    ├── 04_configuration.tex         (fertig, mit Tabelle)
    ├── 05_traffic_analysis.tex       ← 5 Capture-Unterkapitel, je mit [INSERT]-Markierung
    ├── 06_failure_analysis.tex       ← [INSERT]
    ├── 07_multimachine.tex           ← [INSERT]
    ├── 08_reflection.tex             ← Leitfragen + [INSERT]
    └── 09_ai_usage.tex               ← Entwurf, vor Abgabe selbst durchsehen/anpassen
```

Jede rot markierte `\todoblock{...}{...}`-Zeile im PDF zeigt genau, was nach den eigenen Wireshark-Captures noch eingefügt werden muss. Vor der Abgabe alle `\todoblock`-Aufrufe durch echten Text ersetzen.
