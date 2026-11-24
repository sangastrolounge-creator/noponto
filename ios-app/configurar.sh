#!/bin/bash
# Ajusta o projeto iOS gerado pelo Capacitor: textos de permissão, ícone, tela de abertura e nome.
set -euo pipefail
cd "$(dirname "$0")"
P=ios/App/App/Info.plist
pb() { /usr/libexec/PlistBuddy -c "$1" "$P" 2>/dev/null || true; }
set_str() { pb "Delete :$1"; /usr/libexec/PlistBuddy -c "Add :$1 string $2" "$P"; }
set_str CFBundleDisplayName "Data Worke"
set_str NSLocationWhenInUseUsageDescription "O Data Worke confere se você está no local de trabalho quando registra o ponto."
set_str NSCameraUsageDescription "A câmera é usada para conferir o seu rosto no registro de ponto, se a empresa ativou e você aceitou."
set_str NSFaceIDUsageDescription "Use o Face ID para entrar no Data Worke sem digitar a senha."
pb "Delete :ITSAppUsesNonExemptEncryption"; /usr/libexec/PlistBuddy -c "Add :ITSAppUsesNonExemptEncryption bool false" "$P"
pb "Delete :UIRequiresFullScreen"; /usr/libexec/PlistBuddy -c "Add :UIRequiresFullScreen bool true" "$P"
# ícone (1024, sem transparência) e tela de abertura
ICON=$(ls ios/App/App/Assets.xcassets/AppIcon.appiconset/*.png | head -1)
cp recursos/icone-1024.png "$ICON"
for f in ios/App/App/Assets.xcassets/Splash.imageset/*.png; do cp recursos/splash-2732.png "$f"; done
echo "Info.plist ajustado:"; /usr/libexec/PlistBuddy -c Print "$P" | grep -E "Display|Usage|Encryption"
