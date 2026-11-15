# Music Assistant (Spotify)

This Home Assistant app runs Music Assistant with optional developer-app-first Spotify routing and request statistics. Configure Spotify and its routing preference in Music Assistant.

Home Assistant builds the app on the host from the pinned official Music Assistant development image. The upstream installer loads the pinned server and frontend revisions when the app starts. This step needs internet access and can take a few minutes. The Store repository contains source configuration and version pins only.

## Install or move an existing installation

1. Add `https://github.com/KlaysDe/music-assistant-server#ha-store` in the Home Assistant app store's **Repositories** menu, then install **Music Assistant (Spotify)**.
2. To move from the official DEV app, take Home Assistant app-only backups of the stopped DEV app and the fresh, stopped target. Use [`tools/migrate_backup.py`](https://github.com/KlaysDe/music-assistant-server/blob/ha-store/tools/migrate_backup.py) to prepare a restore for the target's full app ID. Run the tool on the Home Assistant host with local, unencrypted archives. The restore keeps the target's add-on options and transfers the Music Assistant settings, library, accounts, and Spotify Soloist pairing.
3. Stop the previous app before starting this one. The two apps use the same host ports. Open the app's web interface and check that players and Spotify are available. If you use a reverse proxy, point it at the new app after verifying the web interface.

Start on boot is off by default so an existing Music Assistant app can keep running while this app is installed. You can enable it after the switch.

## Updates

Each Store version pins the runtime image, server commit, and frontend commit. Updates keep the settings, library, and player configuration in the app's data directory. Apply new versions from **Settings → Updates** in Home Assistant.

[GitHub Actions](https://github.com/KlaysDe/music-assistant-server/actions/workflows/sync-ha-store.yml) checks upstream daily. It reapplies the Spotify patches and publishes a Store version after the targeted server and frontend tests and frontend build pass. A conflict or failed check leaves the previous version available. A manual run leaves the Store version alone unless **Publish** is enabled.

The **Log level** and **Safe mode** options have the same purpose as in the official app. Safe mode starts the server without loading providers.
