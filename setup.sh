#!/bin/bash
# Install YouTube listener and play script on OSMC (run from this repo directory).
# Requires: sudo, systemd, python3, yt-dlp, curl. Kodi must have HTTP control enabled (port 8080).

set -e
INSTALL_DIR=/yt-service
SERVICE_NAME=yt-listener.service

echo "Installing to $INSTALL_DIR (user osmc)..."
sudo mkdir -p "$INSTALL_DIR"
sudo chown osmc:osmc "$INSTALL_DIR"
PY_DEPS=(
  yt-listener.py yt_party_play.py
  kodi_helper.py logger_helper.py kodi_jsonrpc.py ytdlp_helper.py
  party_cache.py instance_control.py stream_cache.py
)
sudo cp -v "${PY_DEPS[@]}" "$INSTALL_DIR/"
for f in "${PY_DEPS[@]}"; do
  sudo chown osmc:osmc "$INSTALL_DIR/$f"
done
sudo cp -a script.stop.yt "$INSTALL_DIR/" 2>/dev/null || true
sudo chown -R osmc:osmc "$INSTALL_DIR/script.stop.yt" 2>/dev/null || true
sudo chmod +x "$INSTALL_DIR/yt_party_play.py"

KODI_ENV="$INSTALL_DIR/kodi-jsonrpc.env"
need_write=true
if sudo test -f "$KODI_ENV"; then
  read -rp "Keep existing Kodi HTTP JSON-RPC credentials ($KODI_ENV)? [Y/n]: " keep
  case "${keep,,}" in n|no) need_write=true ;; *) need_write=false ;; esac
fi
if $need_write; then
  echo "Kodi JSON-RPC uses the same username/password as *Settings → Services → Control* (HTTP)."
  kodi_user=""
  while [ -z "$kodi_user" ]; do
    read -rp "Kodi HTTP username: " kodi_user
    [ -n "$kodi_user" ] || echo "Username cannot be empty."
  done
  read -rsp "Kodi HTTP password: " kodi_pass
  echo
  tmp="$(mktemp)"
  {
    printf 'KODI_JSONRPC_USER=%q\n' "$kodi_user"
    printf 'KODI_JSONRPC_PASS=%q\n' "$kodi_pass"
  } >"$tmp"
  sudo mv "$tmp" "$KODI_ENV"
  sudo chown osmc:osmc "$KODI_ENV"
  sudo chmod 600 "$KODI_ENV"
  echo "Wrote $KODI_ENV (mode 600, not in git)."
fi

echo "Installing systemd service (with INSTALL_DIR=$INSTALL_DIR)..."
sed "s|/home/osmc|$INSTALL_DIR|g" yt-listener.service | sudo tee /etc/systemd/system/yt-listener.service > /dev/null
sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE_NAME"

# Kodi service add-on: Stop → GET /stop-yt (enable once in Kodi UI)
ADDON_SRC="script.stop.yt"
ADDON_DST="/home/osmc/.kodi/addons/script.stop.yt"
if [ -f "$ADDON_SRC/addon.xml" ] && [ -f "$ADDON_SRC/service.py" ]; then
  echo "Installing Kodi add-on $ADDON_SRC → $ADDON_DST ..."
  sudo rm -rf "$ADDON_DST"
  sudo mkdir -p "$ADDON_DST"
  sudo cp -a "$ADDON_SRC/addon.xml" "$ADDON_SRC/service.py" "$ADDON_DST/"
  sudo chown -R osmc:osmc "$ADDON_DST"
  echo "  Restart Kodi, then: Settings → Add-ons → My add-ons → Services →"
  echo "  'Stop YT downloads on Stop' → Enable."
else
  echo "No $ADDON_SRC add-on in repo; skipping."
fi

echo "Done. Start with: sudo systemctl start $SERVICE_NAME"
echo "Send a URL: http://<OSMC-IP>:5000/?url=<YOUTUBE_URL>"
