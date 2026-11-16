# NEXXA UI Screenshot Gallery

This directory contains visual captures of all 12 system modules running with **100 days of seeded demo data** (`seed_demo.run(days=100, seed=1)`).
All captures were generated using **Microsoft Edge headless** (`msedge.exe --headless=new`) at two responsive viewport standards:
- **Desktop**: 1440×900 px (`-1440.png`)
- **Mobile**: 375×812 px (`-375.png`)

---

## Final Production Screenshots Index

| # | Module | Route | Desktop View (1440px) | Mobile View (375px) | Description |
| :---: | :--- | :--- | :--- | :--- | :--- |
| 1 | **Login** | `/login` | [final-login-1440.png](final-login-1440.png) | [final-login-375.png](final-login-375.png) | Nature Glass frosted login panel, credentials form, and light/dark theme toggle. |
| 2 | **Dashboard** | `/` | [final-dashboard-1440.png](final-dashboard-1440.png) | [final-dashboard-375.png](final-dashboard-375.png) | Public greenhouse twin (3D canvas / 2D fallback), live robot position, and real-time station statuses. |
| 3 | **Trends** | `/trends` | [final-trends-1440.png](final-trends-1440.png) | [final-trends-375.png](final-trends-375.png) | 100-day historical health trends, interactive Chart.js graphs, symptom rates, and inspection counts. |
| 4 | **Admin Overview** | `/admin` | [final-admin-1440.png](final-admin-1440.png) | [final-admin-375.png](final-admin-375.png) | Admin control panel summary: active stations, growing batches, key readiness, and quick actions. |
| 5 | **Stations** | `/admin/stations` | [final-stations-1440.png](final-stations-1440.png) | [final-stations-375.png](final-stations-375.png) | Inspection station management list: RFID mapping, coordinates, route ordering, and active crops. |
| 6 | **Crops** | `/admin/crops` | [final-crops-1440.png](final-crops-1440.png) | [final-crops-375.png](final-crops-375.png) | Plant types list (Green Oak, Red Oak, Butterhead, Frillice, Cos), AI vision diagnosis prompts, and symptom label sets. |
| 7 | **Batches (Lots)** | `/admin/batches` | [final-batches-1440.png](final-batches-1440.png) | [final-batches-375.png](final-batches-375.png) | Crop cultivation lots list: station allocation, staggered planting dates, harvest tracking, and age calculations. |
| 8 | **AI Provider Keys** | `/admin/keys` | [final-keys-1440.png](final-keys-1440.png) | [final-keys-375.png](final-keys-375.png) | Fernet-encrypted vision provider keys (Anthropic, OpenAI, Gemini, Qwen, DeepSeek) with masked display. |
| 9 | **Robot Devices** | `/admin/devices` | [final-devices-1440.png](final-devices-1440.png) | [final-devices-375.png](final-devices-375.png) | Inspection carrier device keys list: SHA-256 hashed credentials, active statuses, and revocation management. |
| 10 | **Settings (Stream)** | `/admin/settings` | [final-settings-1440.png](final-settings-1440.png) | [final-settings-375.png](final-settings-375.png) | Carrier robot live stream URL configuration and connection parameters. |
| 11 | **AI Settings** | `/admin/ai` | [final-ai-1440.png](final-ai-1440.png) | [final-ai-375.png](final-ai-375.png) | Active vision provider selection, custom model overrides, base URL validation, and readiness cards. |
| 12 | **Admin Users** | `/admin/users` | [final-users-1440.png](final-users-1440.png) | [final-users-375.png](final-users-375.png) | System administrators management: scrypt password protection, last login timestamps, and account deletion guards. |
| 13 | **Audit Trail** | `/admin/audit` | [final-audit-1440.png](final-audit-1440.png) | [final-audit-375.png](final-audit-375.png) | Paginated audit log table: operator identity, actions, entity identifiers, and sanitized change details. |

---

## Visual Verification Notes

- **Typography & Localization**: All screens render Thai typography cleanly using Google Noto Sans Thai / Plus Jakarta Sans fallback without font overlap or clipping.
- **Glassmorphism Design Tokens**: Foliage background and frosted glass surface cards maintain WCAG AAA contrast ratios (≥ 7:1 for text, ≥ 4.5:1 for interactive elements).
- **Responsive Navigation**: Mobile screens (375px) automatically collapse tables into responsive cards, stack grid layouts, and adjust tap targets to ≥ 44px touch standards.
- **Data Completeness**: With 100 days of demo data seeded, trends charts, station health indicators, and batch age metrics display continuous non-empty state graphs.
