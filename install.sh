#!/usr/bin/env bash
set -euo pipefail

echo "=== H264 VAAPI Encoder – Installationsskript ==="
echo ""

# Detect package manager
if command -v apt-get &>/dev/null; then
    PKG="apt-get"
    INSTALL="sudo apt-get install -y"
    PKGS="python3 python3-gi python3-gi-cairo gir1.2-gtk-3.0 ffmpeg"
elif command -v dnf &>/dev/null; then
    PKG="dnf"
    INSTALL="sudo dnf install -y"
    PKGS="python3 python3-gobject gtk3 ffmpeg"
elif command -v pacman &>/dev/null; then
    PKG="pacman"
    INSTALL="sudo pacman -S --noconfirm"
    PKGS="python python-gobject gtk3 ffmpeg"
else
    echo "WARNUNG: Kein bekannter Paketmanager gefunden."
    echo "Bitte folgende Pakete manuell installieren:"
    echo "  - python3, python3-gi (PyGObject), gtk3, ffmpeg"
    INSTALL=""
    PKGS=""
fi

if [ -n "$INSTALL" ]; then
    echo "Installiere Abhängigkeiten: $PKGS"
    $INSTALL $PKGS
fi

echo ""
echo "Überprüfe ffmpeg-Installation..."
if command -v ffmpeg &>/dev/null && command -v ffprobe &>/dev/null; then
    echo "  ffmpeg: $(ffmpeg -version 2>&1 | head -1)"
    echo "  ffprobe: OK"
else
    echo "  WARNUNG: ffmpeg oder ffprobe nicht gefunden!"
fi

echo ""
echo "VAAPI-Unterstützung prüfen..."
if ls /dev/dri/renderD* &>/dev/null 2>&1; then
    echo "  DRI-Render-Nodes gefunden: $(ls /dev/dri/renderD*)"
else
    echo "  WARNUNG: Keine DRI-Render-Nodes gefunden."
    echo "  VAAPI-Hardware-Beschleunigung möglicherweise nicht verfügbar."
fi

echo ""
echo "Installation abgeschlossen."
echo ""
echo "Starte die Anwendung mit:"
echo "  python3 main.py"
