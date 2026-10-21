# File Organizer

A local, private, free desktop tool that indexes a very large folder (built for ~2 TB and 500,000+ files),
sorts it into a clean structure, and can undo what it did. No network access, no accounts, no telemetry.

## Install the .exe (no Python needed)

Download `FileOrganizer-Setup.exe` (installer) or `FileOrganizer-portable.exe` (single file, no install) from the
**Actions** tab of the GitHub repo: open the latest "Build File Organizer for Windows" run and download the
artifact at the bottom. Unzip it and run the file. The installer needs no administrator rights and adds a Start menu entry.

Windows will warn "Windows protected your PC" because the program is not code-signed (signing costs money).
Click **More info**, then **Run anyway**. Antivirus tools sometimes flag any PyInstaller-built program;
if yours does, use the Python route below instead, which you can read line by line.

To build the .exe yourself on a Windows PC, double-click `build_exe.bat`.

## Setup with Python (Windows)

1. Install **Python 3.10 or newer** from python.org. In the installer keep "tcl/tk and IDLE" ticked (that is tkinter).
2. Open PowerShell in this folder:
   ```powershell
   python -m venv .venv
   .venv\Scripts\activate
   pip install -r requirements.txt
   python -m file_organizer
   ```
   `requirements.txt` holds three optional packages: `pypdf` (text inside PDFs), `Pillow` (photo dates and GPS) and
   `geonamescache` (offline place names). Without them the program still runs and falls back to simpler sorting.
   For iPhone `.heic` photos also run `pip install pillow-heif`. Everything else uses the Python standard library.
3. For **Symlink** mode only: turn on *Settings > Privacy & security > For developers > Developer Mode*
   (or run PowerShell as Administrator). Copy and Move need no special rights.

## Your first run: do it on a small folder

1. Pick a **small test folder** (a few hundred files) as Source and an **empty folder on another drive** as Destination.
2. Press **Analyze**. Nothing is copied, moved or linked. It writes a preview to
   `%LOCALAPPDATA%\FileOrganizer\reports\`: an HTML report, a CSV you can open in Excel, and a text summary.
3. Open **Insights** for charts, **Review** to search every planned move, or **Open full report** for the HTML page.
4. Choose **Copy** and press **Execute**. Check the result.
5. Press **Undo last** and confirm the destination is cleaned up and the source is untouched.
6. Only then point it at the big folder. Keep using **Copy** first; **Move** is the last step, not the first.

## The dashboard

| Page | What it shows |
|---|---|
| Organize | Source and destination, Analyze / Execute / Undo / Cancel, a live progress ring with files per second. |
| Insights | Totals, a donut of where files will go, projects and trips as bar charts, a photo and video timeline, and plain-language findings with tips. |
| Review | Every planned move in a searchable table. Filter by project, page through the list. |
| Rules | Your own if-then rules (file name contains, file type, size) that send files to a folder you choose, for example `Finance/Tax/{year}`. They win over automatic sorting; an empty rule is refused so it cannot grab everything. |
| Settings | Smart sorting switches, **My name(s)**, **Home country**, duplicate and safety options. Saved automatically. |
| Activity | The live log. |

The HTML report is one self-contained file (no internet, no external scripts). It holds the same charts plus a
searchable table of up to 20,000 files; the CSV always has all of them. Set **My name(s)** so your own name never
becomes a project folder, and **Home country** so photos from home are events, not trips.

## Extras

- **Smart rename** (Settings, off by default). For bills, invoices and receipts it reads the text and proposes
  `2026-05-14 ACME Plumbing Ltd 1,240.00.pdf`. A new name is used only when a date and a vendor or amount are found.
  Dates such as `03/02/2026` are read day first. The preview shows every rename before anything happens, and Undo
  restores the old name. Scans with no text layer keep their own name.
- **Watch folder** (Organize page). Every 5 to 60 minutes it checks whether the source folder changed and, if so,
  runs a new Analyze. It never executes by itself.
- **Audit log.** Each execution writes `execution_N_audit_<time>.csv` to the reports folder: time, action, result,
  source and destination of every file.
- **Your rules** live in `%LOCALAPPDATA%\FileOrganizer\sort_rules.json` if you prefer to edit them by hand.

## What it does

| Stage | Detail |
|---|---|
| Scan | Streams the folder with `os.scandir`, writes rows to SQLite in batches of 5,000. Memory stays flat (about 120 MB for 150,000 files in testing). |
| Metadata | Extension, size, creation and modification time, MIME type (guessed from the extension). |
| Categories | Documents, Graphics-Visuals, Archives, Executables-System, Other (rules in `file_organizer/config.py`). |
| Project folders | Groups files by what they belong to, using names, folders, codes and text (see below). |
| Document keywords | Looks in the file name and, for `.txt .md .csv .pdf .docx .xlsx .pptx`, in the text. Sub-folders: Invoices, Tax, Receipts, Contracts, Projects, else General. A keyword in the name always counts; in the text it must appear twice. |
| Smart photo folders | Reads each photo's capture date and GPS position from its EXIF data, looks the country up in an offline table (no internet), and groups photos from one country with no gap over 10 days into a trip. See below. |
| Duplicates | Only files with identical sizes are compared. A first+last 64 KB fingerprint comes next. Only the survivors are read in full (SHA-256 or MD5). The oldest copy is kept in place; extra copies go to `_Duplicates/` (switchable). Hashes are cached, so a second dry run is faster. |
| Plan | Stored in SQLite and in the CSV. Name clashes get ` (1)`, ` (2)` suffixes. Files are never overwritten. |
| Execute | **Copy**: copies to a temp name, checks size (and hash if you tick Verify), then renames into place. **Move**: instant rename on the same drive; on another drive, copy, verify, then delete the source. **Symlink**: creates links, originals stay put. |
| Undo | Every action is written to a journal *before* it happens. Undo reverses the last run, including a cancelled or crashed one. It removes only copies and links, moves files back, and deletes only empty folders it created. |

## Project folders: files that belong together go together

Instead of sorting by file type, the program works out *what a file belongs to* (a property, a client, a job)
and groups everything about it, for example `Projects\\Orchidia\\Acme Ltd\\Drafts`, `...\\Payments & Banking`,
`...\\Design & Drawings`, `...\\Title & Legal`. It decides from four kinds of evidence:

1. **File names**: shared distinctive words and reference codes (`RX-48213`, `plot917`).
2. **Folder names** you already made (a descriptive folder like `Acme Ltd` counts; `Downloads`, `New folder` do not).
3. **Text inside** PDF, Word, Excel, PowerPoint, HTML, JSON and text files. A single mention of a known project
   name in a contract is enough to link it.
4. **Versions of one document**: a signed copy and its draft stay together even if only the draft has readable text.

Greek and Latin spellings are treated as the same word (`Ορχιδιά`, `Orchidia`, `Orchidia`), and a Greek letter
that looks like a Latin one inside a code (`RΧ-48213` with a Greek Χ) still matches `RX-48213`.

Inside a project the sub-folder is a **document role** read from the name first, then the text: Contracts &
Agreements, Payments & Banking, Identity & KYC, Design & Drawings, Title & Legal, Tax & Government,
Correspondence, Reports & Summaries. A meaningful folder you already had inside the project (3+ files) is
kept instead (`Acme Ltd`). Drafts go in a `Drafts` sub-folder. Files with no evidence stay in the normal type folders.
Every file in the CSV report has `project`, `role` and `why_grouped` columns so you can check each decision.

**Your own rules (optional).** Create `%LOCALAPPDATA%\\FileOrganizer\\projects.json`:

```json
{
  "Orchidia Plot": ["orchidia", "ορχιδιά", "plot 917"],
  "Kitchen Renovation": ["kitchen", "oak cabinets"],
  "ignore": ["john", "smith"]
}
```
A file containing any alias goes to that project. Words under `ignore` never link files: put your own name there,
or it will form a "project" out of your ID papers and statements.

What it cannot do: **read scanned pages.** A scanned ID, signed contract or photographed receipt has no text, so
only its name and folder count. In a flat folder, a scan called `scan0042.pdf` cannot be placed. OCR (reading text
from images) is not built in. Small accidental groups also happen: two unrelated files that share a rare word. The
tuning values (`MIN_PROJECT_FILES`, `MERGE_RATIO`, `BRIDGE_RATIO`) are in `file_organizer/config.py`.

Measured on synthetic data: 300 projects (65,000 files) in one flat folder were all grouped correctly in 44
seconds using about 140 MB of memory; about 1.6% of 20,000 unrelated files were pulled into small accidental
groups. On a real 25-file property folder with its folders intact, all 25 files were grouped as the owner had done by
hand; flattened into one folder, 21 of 25 were grouped and the 4 unreadable scans stayed out.

## Smart photo folders

For camera photos (`.jpg .jpeg .tif .tiff .heic` and RAW such as `.dng .cr2 .nef .arw`) that have a capture date, the folder is chosen in this order:

1. **GPS found, taken abroad, 3+ photos in that country** -> `Photos\Travel\Germany 2025`.
   Two separate trips to one country in a year get the month: `Germany 2025-03`, `Germany 2025-10`.
2. **Photo sits in a meaningfully named folder** (for example `Birthday Party`) -> `Photos\Events\Birthday Party 2024`.
   Generic names such as `DCIM`, `100APPLE`, `Camera`, `New folder (3)`, `Downloads` are ignored.
3. **Date only** -> `Photos\2025\2025-07`. A date in the file name (`IMG-20250714-WA0003.jpg`) counts too.

Videos (`.mp4 .mov` and similar) and RAW files without EXIF are dated from the file name (`DJI_20260927...`) or the
file's modified time. A RAW file or sidecar (`.srt .xmp .lrf`) borrows the date and GPS of the JPEG with the same name
in the same folder. Inside a trip or event, RAW files go to a `RAW` subfolder and videos to a `Videos` subfolder.
Photos or videos in a folder that carries a project's name (for example `Orchidia Drone 27.09.2026`) join that project
instead: `Projects\Orchidia\Drone 27.09.2026\`. Media with no GPS dated within 2 days of a trip join the trip.

Photos with no date and no GPS keep the normal `Graphics-Visuals\JPG` folder, as do PNG, GIF, SVG and PSD files.
Set **Home country code** (for example `GB`) so photos taken at home are not called trips.
The tuning values (`TRIP_GAP_DAYS`, `MIN_TRIP_PHOTOS`, `MAX_GEOCODE_KM`) are in `file_organizer/config.py`.

What this cannot do: it does not look at what is *in* the picture. A photo with no GPS, such as one received through
WhatsApp or downloaded from the web, cannot be placed in "Germany" automatically. Country comes from the nearest
town, so a photo taken right at a border can land in the neighbouring country. A trip name is a country and a year,
not a city or an event name.

Output layout: `Destination\Documents\Invoices\...`, `Destination\Graphics-Visuals\PNG\...`,
`Destination\Archives\ZIP\...`, `Destination\_Duplicates\...`.

## Safety rules built in

- **Dry run is mandatory.** The Execute button stays disabled until a completed dry run exists for the current source and destination.
- **Stale plan check.** If a file changed or vanished after the dry run, it is skipped, not touched.
- **Executables and system files (`.exe .msi .dll .bat`) are flagged but left in place by default.** Moving DLLs out of an installed program breaks the program. Tick the option only for downloads you know are loose.
- **Skipped on purpose:** `.git`, `node_modules`, `Windows`, `Program Files`, `$RECYCLE.BIN`, `System Volume Information`, `AppData`, symlinks and junctions, and OneDrive-style cloud-only placeholders (reading them would download them). Edit `SKIP_DIR_NAMES` in `config.py` to change this.
- The destination may not be inside the source, or the reverse.
- Copy mode checks free space first. Symlink mode checks it is allowed before touching anything.
- A disk-full error or 50 failures in a row stops the run.
- Cancel stops cleanly between files. A file already being copied finishes first.

## Know the limits

- **Move flattens folder structure.** A project folder moved file by file stops being a project. Use Copy, or exclude project folders.
- **Time at 2 TB is dominated by your disk.** Scanning reads metadata only (fast). Hashing reads file contents, but only for size-matching candidates. On a spinning disk set Threads to 1 or 2.
- The database is large: about 1.3 KB per file, so roughly 700 MB for 500,000 files. Old dry runs for the same folders are deleted automatically.
- MIME type comes from the extension, not from file contents.
- Moving a file to another drive needs space for the copy until the original is deleted.

## Headless mode (also handy for scripting)

```powershell
python -m file_organizer dryrun D:\Downloads E:\Sorted
python -m file_organizer execute D:\Downloads E:\Sorted --mode copy
python -m file_organizer undo
```

## Tests

```powershell
python -m unittest discover -s tests -v
```
