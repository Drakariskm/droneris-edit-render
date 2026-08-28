DRONERIS RENDER BACKEND R1.0.0
================================

NAMENA
- Poseban Render Web Service za DRONERIS EDIT.
- Prima MP4/MOV, opciono KMZ/SRT/manifest.
- KMZ/SRT/manifest su READ ONLY. Nema upisa u Core ili misiju.
- ffprobe cita video metadata.
- Server vraca R1 First Cut plan (7 scena / cilj 75 s).
- FFmpeg renderuje izabrane scene u 1920x1080 / 30 fps H.264 MP4.
- Podrzani su trim, speed i centralni crop/zoom iz trenutnog EDIT plana.
- Opcioni user music fajl se loopuje i fade-uje preko finalnog videa.

API
GET  /health
POST /api/jobs
GET  /api/jobs/{jobId}
POST /api/jobs/{jobId}/render
GET  /api/jobs/{jobId}/download

RENDER SETUP
1. Napravi Git repo samo sa sadrzajem ovog foldera.
2. Render Dashboard -> New -> Web Service.
3. Povezi repo.
4. Runtime: Docker (Render ce procitati Dockerfile).
5. Service name: droneris-edit-render (ako je slobodno).
6. Instance: Free za test.
7. Health Check Path: /health
8. Environment:
   ALLOWED_ORIGINS=https://edit.droneris.tech
   DRONERIS_MAX_PARALLEL_RENDERS=1
   DRONERIS_JOB_TTL_SECONDS=21600
9. Deploy.
10. Posle deploy-a otvori https://<service>.onrender.com/health i proveri ok=true.

VAZNO ZA FREE/TEST
- Obrada se radi u /tmp i fajlovi su privremeni.
- Render Free filesystem nije trajna arhiva; ovo je processing prostor, ne storage.
- Free servis moze da se uspava posle neaktivnosti; prvi poziv tada ima cold start.
- Za produkciju cemo finalne/originalne fajlove prebaciti na object storage i po potrebi jaci compute.

SLEDECI KORAK
Kada dobijemo tacan Render URL, patchujemo edit.droneris.tech da:
1) NAPRAVI PRVI REZ -> POST /api/jobs
2) koristi server scenes umesto lokalnog placeholdera
3) PREUZMI FINAL VIDEO -> POST /render, poll GET /api/jobs/{id}, zatim /download

R1 ogranicenje
- AI vision/content ranking jos nije povezan; First Cut je server-side Director baseline.
- Fizicki FFmpeg render je stvaran i izvrsan.
