# Music Assistant (Spotify)

This Home Assistant app runs Music Assistant with optional developer-app-first Spotify routing and request statistics. Configure Spotify and its routing preference in Music Assistant.

Home Assistant builds the app on the host from the pinned official Music Assistant development image. The upstream installer loads the pinned server and frontend revisions when the app starts. This step needs internet access and can take a few minutes. The Store repository contains source configuration and version pins only.

## Install or move an existing installation

1. Add `https://github.com/KlaysDe/music-assistant-server#ha-store` in the Home Assistant app store's **Repositories** menu, then install **Music Assistant (Spotify)**.
2. For an existing Music Assistant setup, back up the current app and migrate its data to the new app before the first start. Home Assistant assigns the new app its own storage. The data includes player configuration and the Spotify Soloist pairing.
3. Stop the previous app before starting this one. The two apps use the same host ports. Open the app's web interface and check that players and Spotify are available. If you use a reverse proxy, point it at the new app after verifying the web interface.

Start on boot is off by default so an existing Music Assistant app can keep running while this app is installed. You can enable it after the switch.

## Updates

Each Store version pins the runtime image, server commit, and frontend commit. Updates to this same app keep its settings, library, and player configuration in its existing data directory. Use the Store update button when a new version is available.

The **Log level** and **Safe mode** options have the same purpose as in the official app. Safe mode starts the server without loading providers.
