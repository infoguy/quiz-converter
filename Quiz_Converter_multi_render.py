# quiz_creator.py
# Canvas Quiz Converter (text -> QTI zip via text2qti)
# Behavior change: when validation fails, UI shows ONLY the validation messages
# (e.g., "Line 7: No correct answer marked. Add * to exactly one option.")
# Non-validation errors show a short generic message in UI, while full traceback prints to console.

print("=== RUNNING QUIZ CREATOR (VALIDATION-FRIENDLY BUILD) ===")
print("=== VERSION: TITLE-PATCH + FILL-IN-BLANK + MATCHING ORDER-PRESERVED + ANSWER FEEDBACK ===")

from flask import Flask, request, jsonify, Response, render_template_string
from pathlib import Path
from tempfile import TemporaryDirectory
import io
import re
import subprocess
import sys
import threading
import time
import urllib.parse
import uuid as _uuid
import webbrowser
import traceback
import zipfile
from typing import List, Tuple, Dict

# Hide console window on Windows (for use with --windowed PyInstaller builds)
if sys.platform == "win32":
    import ctypes
    ctypes.windll.user32.ShowWindow(ctypes.windll.kernel32.GetConsoleWindow(), 0)

import os

IS_RENDER = "RENDER" in os.environ

# Redirect stdout/stderr to a log file so errors aren't silently lost when
# running as a local desktop/PyInstaller build. On Render, skip this: Render
# captures stdout/stderr for its dashboard log viewer, so redirecting to a
# file would make your app's logs (and any crash tracebacks) invisible there.
if not IS_RENDER:
    _log_path = os.path.join(os.path.dirname(os.path.abspath(
        sys.executable if getattr(sys, 'frozen', False) else __file__
    )), "quiz_converter.log")
    sys.stdout = open(_log_path, "w", buffering=1, encoding="utf-8")
    sys.stderr = sys.stdout


app = Flask(__name__)

HOST = "0.0.0.0"
PORT = int(os.environ.get("PORT",8011))

# --- text2qti is used as an in-process library, NOT as an external executable. ---
# This is the key change that makes the converter work on ANY machine: there is
# no .exe to locate, no PATH lookup, no per-user install folder to guess at.
# As long as the "text2qti" Python package is installed in the same environment
# this script runs in (or bundled into the PyInstaller build), conversion works
# identically for every user, regardless of username or OS.
TEXT2QTI_IMPORT_ERROR = None
try:
    from text2qti.quiz import Quiz as _T2QQuiz
    from text2qti.qti import QTI as _T2QQTI
    from text2qti.config import Config as _T2QConfig
    from text2qti.err import Text2qtiError
except Exception as _e:  # pragma: no cover - only triggered if text2qti isn't installed
    TEXT2QTI_IMPORT_ERROR = str(_e)

    class Text2qtiError(Exception):
        """Fallback so the rest of the module still imports cleanly."""
        pass


HTML = """<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <title>Canvas Quiz Converter</title>
  <style>
    body{font-family:sans-serif;max-width:900px;margin:2rem auto;padding:0 1rem;line-height:1.5;}
    .card{border:1px solid #ddd;border-radius:10px;padding:1.5rem 1.5rem 2rem;}
    button{padding:.5rem 1rem;border:1px solid #000;background:#000;color:#fff;border-radius:6px;cursor:pointer;}
    input[type=file]{margin-bottom:1rem}
    .dropzone{border:2px dashed #1d4ed8;background:#eff6ff;padding:1.25rem 1rem;border-radius:.75rem;margin:1rem 0 1.5rem;}
    code{background:#f3f4f6;padding:0 .25rem;border-radius:.25rem;}
    pre{background:#f3f4f6;padding:.75rem;border-radius:.5rem;overflow-x:auto;white-space:pre-wrap;}
    .err{display:none;border:1px solid #fecaca;background:#fef2f2;color:#7f1d1d;padding:1rem;border-radius:.75rem;margin:1rem 0;overflow-wrap:anywhere;word-break:break-word;}
    a{color:#1d4ed8}
  </style>
</head>
<body>
  <div class="card">
    <h1>Canvas Quiz Converter</h1>
    <p>Upload a plain-text quiz file and convert it to a Canvas-compatible QTI .zip. Questions can include images.</p>

    <div class="dropzone">
      <strong>1.</strong> Choose one or more quiz <code>.txt</code> files (hold Ctrl/Cmd to select multiple).<br>
      <strong>2.</strong> If any question uses an image, select the image files in the <em>same</em> upload.<br>
      <strong>3.</strong> All files convert and download together as a single <code>QTI_Bundle.zip</code>.
    </div>

    <div id="err" class="err">
      <strong>Conversion failed. Details:</strong>
      <pre id="errMsg" style="margin:.75rem 0 0;background:#fff;padding:.75rem;border-radius:.5rem;white-space:pre-wrap;overflow-wrap:anywhere;word-break:break-word;"></pre>
      <a href="#" id="tryAgain">Try Again</a>
    </div>

    <form id="f" method="post" enctype="multipart/form-data">
      <input type="file" name="files" accept=".txt,.png,.jpg,.jpeg,.gif,.webp,.bmp,.svg" multiple required />
      <button type="submit">Convert &amp; Download</button>
    </form>

    <h2>Canvas Quiz Converter Directions</h2>
    <ol>
      <li>Upload your plain text quiz file to convert it to a Canvas QTI format.</li>
      <li>If any question uses an image, select the image files at the same time as the quiz file (hold Ctrl/Cmd and click each one). The images are packaged inside the .zip.</li>
      <li>In Canvas: <strong>Settings</strong> &rarr; <strong>Import Course Content</strong>.</li>
      <li>Select <strong>QTI .zip file</strong> &rarr; locate and choose the zipped file generated by the converter.</li>
      <li>Click <strong>Import</strong>.</li>
      <li>You do not need to select a question bank; that will be managed automatically.</li>
      <li>Any images land in the course <strong>Files</strong> area, inside an <code>images</code> folder, and are linked from the questions automatically.</li>
      <li>Preview the quiz before releasing to students, and confirm the images display.</li>
    </ol>

    <h2>Instructions for Formatting Guide for Questions</h2>
    <p>This tool supports <strong>Multiple Choice</strong>, <strong>True/False</strong>, <strong>Multiple Answers</strong>, <strong>Essay</strong>, <strong>Fill in the Blank</strong>, <strong>Matching</strong>, and <strong>Question Groups</strong>. Any question type can include an <strong>image</strong> and <strong>answer feedback</strong>. Use the exact formats shown.</p>

    <pre>1. What is 2+3?
a) 6
b) 1
*c) 5

2. 2+3 is 5.
*a) True
b) False

3. Which of the following are dinosaurs?
[ ] Woolly mammoth
[*] Tyrannosaurus rex
[*] Triceratops
[ ] Smilodon fatalis

4. Write an essay.
___

5. The color of the sky is ____.
* blue
* Blue

6. Match each medication route to its definition.
= Sublingual -> Dissolved under the tongue
= Intradermal -> Injected into the dermis layer of skin
= Transdermal -> Absorbed through a patch on the skin

7. Which port is shown below?
![rear panel of a motherboard](motherboard.png)
a) HDMI
*b) DisplayPort
c) VGA
d) DVI</pre>

    <h3>Answer Feedback</h3>
    <p>Feedback lines let you tell the student <em>why</em> an answer was right or wrong. There are three markers, and each one goes on its own line starting at the left margin with a space after the marker:</p>
    <table style="border-collapse:collapse;margin:1rem 0;">
      <tr><td style="padding:.35rem .75rem .35rem 0;"><code>...</code></td><td style="padding:.35rem 0;">Feedback for whichever line sits directly above it: the question text, or a single answer choice.</td></tr>
      <tr><td style="padding:.35rem .75rem .35rem 0;"><code>...*</code></td><td style="padding:.35rem 0;">The same feedback on every answer choice. Written once, under the question text.</td></tr>
      <tr><td style="padding:.35rem .75rem .35rem 0;"><code>+</code></td><td style="padding:.35rem 0;">Shown to students who answered the question correctly.</td></tr>
      <tr><td style="padding:.35rem .75rem .35rem 0;"><code>-</code></td><td style="padding:.35rem 0;">Shown to students who answered the question incorrectly.</td></tr>
    </table>
    <p><strong>Feedback for each answer choice.</strong> Put a <code>...</code> line directly beneath the choice it belongs to. Choices without a feedback line simply have none:</p>
    <pre>1. Which port carries both video and audio?
...  Think about which connector replaced DVI on modern monitors.
+    Correct. HDMI carries video and audio over one cable.
-    Review the section on display connectors in Module 3.
a) VGA
...  VGA is analog and video only, so audio needs a separate cable.
b) DVI
...  DVI carries video only. Some versions add digital signaling, but never audio.
*c) HDMI
...  Right. HDMI carries digital video and audio together.
d) PS/2
...  PS/2 is a keyboard and mouse connector, not a display connector.</pre>
    <p><strong>Where each marker goes.</strong> The <code>...</code> general line and the <code>+</code> and <code>-</code> lines all belong directly under the question text, above the first answer choice. Once the answer choices start, a <code>...</code> line attaches to the choice above it instead. You can use any of them on their own; nothing is required.</p>

    <p><strong>One rationale on every answer choice.</strong> When the same explanation should appear on all of the choices, write it once with <code>...*</code> under the question text. The converter copies it into the feedback box of every choice, so the student sees the rationale no matter which answer they picked:</p>
    <pre>1. Which Medicare part covers inpatient hospital stays and skilled nursing facility care?
...* Part A = hospital/facility insurance covering inpatient stays, SNF, hospice, and home health. Part B = outpatient/physician. Part C = Medicare Advantage. Part D = prescription drugs.
*a) Part A
b) Part B
c) Part C
d) Part D</pre>
    <p>That produces the same result as typing the identical <code>...</code> line under all four choices. If one choice needs its own wording, give that choice a <code>...</code> line of its own; the specific line wins and the shared text fills in the rest.</p>

    <p><strong>Other question types.</strong> Feedback works the same way everywhere:</p>
    <pre>2. Select all storage devices.
...  Storage keeps data when the power is off.
[*] SSD
...  Correct, an SSD stores data on flash memory.
[ ] RAM
...  RAM is volatile memory, so it clears at shutdown.
[*] Hard drive

3. The color of the sky is ____.
+    Correct.
-    Look again at the section on light scattering.
* blue
* Blue

4. Write an essay describing the boot process.
...  Cover POST, the bootloader, and kernel handoff.
___

5. Match each medication route to its definition.
...  Focus on where the medication enters the body.
+    Well done, all three routes matched correctly.
-    Review the routes of administration table.
= Sublingual -> Dissolved under the tongue
...  Sub means under and lingual means tongue.
= Intradermal -> Injected into the dermis layer of skin
= Transdermal -> Absorbed through a patch on the skin</pre>
    <p><strong>Rules:</strong></p>
    <ul>
      <li>Start the marker at the left margin. An indented feedback line is treated as part of the line above it.</li>
      <li>A space after the marker is preferred, and the converter adds one for you if you type <code>...Correct</code> instead of <code>... Correct</code>.</li>
      <li>Keep each piece of feedback on one line, and use one <code>...</code> line per answer choice.</li>
      <li>Essay questions accept <code>...</code> only, since there is no right or wrong answer to score.</li>
      <li>Feedback is optional. Add it to one question, a few answers, or none at all.</li>
      <li>Because <code>-</code> starts an incorrect-answer feedback line, do not begin an ordinary line with a dash and a space unless you mean it as feedback.</li>
      <li>Students see the feedback after the quiz according to the quiz settings in Canvas, so check <strong>Let Students See The Correct Answers</strong> and the response options when you publish.</li>
    </ul>

    <h3>Fill in the Blank Format</h3>
    <p>Write the question with <code>____</code> (four underscores) where the blank should appear. Then list each accepted answer on its own line starting with <code>*</code>. Answers are case-sensitive unless you list both variants.</p>
    <pre>5. The color of the sky is ____.
* blue
* Blue

6. Water boils at ____ degrees Celsius.
* 100</pre>
    <p><strong>Tips:</strong> Add multiple <code>*</code> lines to accept alternate spellings or capitalizations. The student will see a text box to type their answer.</p>

    <h3>Matching Format</h3>
    <p>Write the question text on the first line, then list each pair on its own line using <code>=</code> followed by the left-side term, <code>-&gt;</code>, and the right-side match.</p>
    <pre>6. Match each medication route to its definition.
= Sublingual -> Dissolved under the tongue
= Intradermal -> Injected into the dermis layer of skin
= Transdermal -> Absorbed through a patch on the skin</pre>
    <p>To include a picture, put it on the question text line or on its own line directly beneath it, above the pairs:</p>
    <pre>6. Match each medication route to its definition.
![chart of medication routes](routes.png)
= Sublingual -> Dissolved under the tongue
= Intradermal -> Injected into the dermis layer of skin
= Transdermal -> Absorbed through a patch on the skin</pre>
    <p><strong>Rules:</strong></p>
    <ul>
      <li>Each pair must be on its own line starting with <code>=</code>.</li>
      <li>Separate left and right sides with <code>-&gt;</code> (dash + greater-than).</li>
      <li>You need at least 2 pairs per question.</li>
      <li>Left-side terms must be unique within a question.</li>
      <li>Both sides of <code>-&gt;</code> must have text.</li>
      <li>In Canvas, students see the left-side terms and select the correct right-side match from a dropdown.</li>
      <li>An image can go in the question text line, or on its own line directly beneath it. Images cannot go inside the pairs, because Canvas renders the terms and the dropdown choices as plain text.</li>
    </ul>

    <h3>Images in Questions</h3>
    <p>Add an image with <code>![description](filename.png)</code>. Put it on the same line as the question text, or on the line directly below it. It works in every question type: the question stem, an answer choice, and the text of a matching question.</p>
    <pre>1. Which port is shown below?
![rear panel of a motherboard](motherboard.png)
a) HDMI
*b) DisplayPort
c) VGA
d) DVI</pre>
    <p><strong>Images in the answer choices.</strong> An image can be the whole choice, or sit alongside the choice text. Put it after the <code>a)</code> / <code>[ ]</code> marker on the same line. This works for multiple choice, true/false, and multiple answers, and the correct-answer markers (<code>*</code> and <code>[*]</code>) are used exactly as they normally are:</p>
    <pre>1. Which topology is shown?
![network diagram](stem.png)
*a) ![star topology](a1.png)
b) ![ring topology](a2.png)
c) Neither one

2. Select all that apply.
[*] ![one](m1.png)
[ ] ![two](m2.png)
[*] Option C with text and a picture ![three](m3.png)</pre>
    <p>Note that a question can use images in the stem and in the choices at the same time, and choices can be mixed: some with images, some plain text. Every image named in the file has to be selected in the upload alongside the quiz <code>.txt</code>.</p>

    <p>Matching questions work the same way:</p>
    <pre>6. Match each medication route to its definition.
![chart of medication routes](routes.png)
= Sublingual -> Dissolved under the tongue
= Intradermal -> Injected into the dermis layer of skin
= Transdermal -> Absorbed through a patch on the skin</pre>
    <p><strong>Rules:</strong></p>
    <ul>
      <li>Works in every question type, including matching. In a matching question the image goes in the question text line or on its own line above the pairs, never inside a pair.</li>
      <li>Answer choices can hold images for multiple choice, true/false, and multiple answers. Keep the image on the same line as the <code>a)</code> or <code>[ ]</code> marker; a choice marker with nothing after it is not recognized as an option.</li>
      <li>If several choices point at the same picture, upload it once and reference the same file name in each choice.</li>
      <li>Select the image files together with the quiz <code>.txt</code> file when you upload. The converter bundles them into the QTI package, and Canvas copies them into the course files folder on import.</li>
      <li>The name in the quiz text must match the image file name, including the extension.</li>
      <li>Supported: <code>.png</code>, <code>.jpg</code>, <code>.jpeg</code>, <code>.gif</code>, <code>.webp</code>, <code>.bmp</code>, <code>.svg</code>.</li>
      <li>The text inside the square brackets becomes the alt text, so write something descriptive for accessibility.</li>
      <li>You can also point to an image already on the web: <code>![alt](https://example.com/image.png)</code>. Nothing gets bundled in that case, so the address has to be reachable by students.</li>
      <li>If you see <em>"Image file(s) not found"</em>, the picture was left out of the upload or the file name in the quiz text does not match. Re-select the quiz file and every image together and convert again.</li>
      <li>If you see <em>"Images cannot be used inside matching pairs"</em>, an image was placed on one side of a <code>-&gt;</code>. Move it up to the question text line.</li>
    </ul>

    <h3>Making Questions Worth More Than 1 Point</h3>

    <p><strong>Option 1: put "Points: X" right above the question.</strong> Example:</p>
    <pre>Points: 2
1. What is the act of adding fluid, such as distilled water, to a powdered or crystalline form of medication to make a specific liquid dosage strength?
a) Dissolution
b) Resolution
*c) Reconstitution
d) Reconstruction

Points: 2
2. The majority of stock liquid measurement strengths are listed as
*a) mg/mL
b) g/mL
c) tsp/mL
d) All are used about the same amount.</pre>

    <p><strong>Option 2: edit the generated QTI file.</strong></p>
    <ol>
      <li>Unzip the exported quiz .zip.</li>
      <li>Open <code>text2qti_assessment.xml</code> in a text editor.</li>
      <li>Press <strong>Ctrl+H</strong> (find &amp; replace).</li>
      <li>Find <code>&lt;fieldentry&gt;1&lt;/fieldentry&gt;</code> and replace with <code>&lt;fieldentry&gt;2&lt;/fieldentry&gt;</code> (or whatever point value you want).</li>
      <li>Save, re-zip, and import to Canvas.</li>
    </ol>
  </div>

  <script>
    document.getElementById('f').addEventListener('submit', async (e)=>{
      e.preventDefault();
      document.getElementById('err').style.display = 'none';

      const fileInput = e.target.querySelector('input[type=file]');
      const files = Array.from(fileInput.files);
      if (!files.length) return;

      const btn = e.target.querySelector('button[type=submit]');
      btn.disabled = true;
      btn.textContent = `Converting ${files.length} file${files.length > 1 ? 's' : ''}…`;

      const fd = new FormData();
      for (const file of files) fd.append('files', file);

      let res;
      try {
        res = await fetch('/', {method:'POST', body: fd});
      } catch (networkErr) {
        document.getElementById('errMsg').textContent = `Network error: ${networkErr.message}`;
        document.getElementById('err').style.display = 'block';
        btn.textContent = 'Convert & Download';
        btn.disabled = false;
        return;
      }

      if (!res.ok) {
        let msg = 'Conversion failed';
        try { const j = await res.json(); if (j && j.reason) msg = j.reason; } catch {}
        document.getElementById('errMsg').textContent = msg;
        document.getElementById('err').style.display = 'block';
        btn.textContent = 'Convert & Download';
        btn.disabled = false;
        return;
      }

      const blob = await res.blob();
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      const disposition = res.headers.get('Content-Disposition') || '';
      const match = disposition.match(/filename=(.+)/);
      a.download = match ? match[1] : 'QTI_Bundle.zip';
      document.body.appendChild(a);
      a.click();
      a.remove();

      btn.textContent = 'Convert & Download';
      btn.disabled = false;
    });

    document.getElementById('tryAgain').addEventListener('click', (e)=>{
      e.preventDefault();
      document.getElementById('err').style.display = 'none';
    });
  </script>
</body>
</html>
"""


# ---------------- Exceptions ----------------

class ValidationError(Exception):
    """Raised when quiz formatting validation fails."""
    pass


# ---------------- Parsing / validation ----------------

Q_START_RE = re.compile(r"^\s*(\d+)\.\s+(.*\S)\s*$")
POINTS_RE = re.compile(r"^\s*Points\s*:\s*(\d+)\s*$", re.I)
MC_OPT_RE = re.compile(r"^\s*\*?\s*([a-eA-E])\)\s+(.+?)\s*$")
# Multiple answers: "[*] correct option" / "[ ] incorrect option".
# The space inside the brackets is optional, matching what text2qti accepts.
MA_OPT_RE = re.compile(r"^\s*\[\s*(\*?)\s*\]\s+(.+?)\s*$")
# Fill-in-the-blank: correct answer line starts with * followed by space + text
FITB_ANS_RE = re.compile(r'^\s*\*\s+(.+?)\s*$')
# Matching pair: lines starting with "= left -> right"
MATCH_PAIR_RE = re.compile(r'^\s*=\s*(.+?)\s*->\s*(.+?)\s*$')

# ---------------- Answer feedback ----------------
# Feedback lines use the same markers text2qti understands. The marker has to
# start at the left margin (no leading spaces) and be followed by a space:
#   ...  text   general feedback for the question when it sits directly under
#               the question text, or feedback for the ONE answer choice
#               directly above it
#   +    text   feedback shown when the student gets the question right
#   -    text   feedback shown when the student gets the question wrong
FEEDBACK_RE = re.compile(r'^(\.\.\.|\+|-)[ \t]+(\S.*?)\s*$')
# Marker present but nothing written after it
FEEDBACK_EMPTY_RE = re.compile(r'^(\.\.\.|\+|-)[ \t]*$')
# One feedback line that applies to EVERY answer choice in the question:
#   ...* text
# Written once under the question text; the converter copies it beneath each
# choice, which is what Canvas shows in the "Answer Feedback" box for each one.
SHARED_FEEDBACK_RE = re.compile(r'^\.\.\.\*[ \t]*(\S.*?)\s*$')
# Marker written flush against its text ("...text"). text2qti rejects that, so
# the converter quietly inserts the space instead of making the instructor fix
# every line by hand. A bare "-" is left alone so ordinary dashes and negative
# numbers in a question are not mistaken for feedback.
FEEDBACK_NOSPACE_RE = re.compile(r'^(\.\.\.\*|\.\.\.(?!\*)|\+)(?=\S)')

FEEDBACK_LABELS = {"...": "general", "+": "correct-answer", "-": "incorrect-answer"}


def is_answer_line(t: str) -> bool:
    """True if the line is an answer option / essay blank / matching pair."""
    return bool(
        MC_OPT_RE.match(t)
        or MA_OPT_RE.match(t)
        or FITB_ANS_RE.match(t)
        or MATCH_PAIR_RE.match(t)
        or t.strip() in ("___", "____")
    )


def normalize_feedback_markers(text: str) -> str:
    """
    Cleans up the ways a feedback marker actually arrives from a real quiz file
    before anything tries to read it:

      * a leading byte-order mark from a file saved out of Word or Notepad
      * the single ellipsis character Word and Google Docs autocorrect "..." into
      * a marker typed flush against its text ("...Part A is hospital insurance")
      * the shared marker typed with a space in it ("... * text")
      * a feedback line that got indented, which would otherwise be swallowed
        into the answer choice above it
      * a non-breaking space after the marker

    Feedback lines end up flush at the left margin with one space after the
    marker, which is the only form text2qti reads.
    """
    if text.startswith("\ufeff"):
        text = text[1:]

    out = []
    for ln in text.splitlines():
        s = ln.lstrip(" \t\u00a0")

        # Word and Google Docs replace three periods with one ellipsis glyph
        if s[:1] == "\u2026":
            s = "..." + s[1:]

        if s.startswith("..."):
            s = s.replace("\u00a0", " ")
            # "... * text" typed with a space means the shared marker
            m_shared = re.match(r'^\.\.\.[ \t]*\*[ \t]+(\S.*)$', s)
            if m_shared:
                s = "...* " + m_shared.group(1)
            m_nospace = FEEDBACK_NOSPACE_RE.match(s)
            if m_nospace:
                marker = m_nospace.group(1)
                s = marker + " " + s[len(marker):].strip()
            # A feedback line belongs at the left margin, never indented
            out.append(s)
            continue

        m = FEEDBACK_NOSPACE_RE.match(ln)
        if m:
            marker = m.group(1)
            out.append(marker + " " + ln[len(marker):].strip())
            continue

        out.append(ln)
    return "\n".join(out)


def expand_shared_choice_feedback(text: str) -> str:
    """
    Turns a single "...* text" line under a question into a "... text" line
    beneath every answer choice in that question, which is how Canvas fills in
    the per-answer feedback box. A choice that already carries its own "..."
    line keeps it; the shared text only fills the gaps.

    Run this AFTER validation so reported line numbers match the source file.
    """
    lines = text.splitlines()
    blocks = split_questions(lines)

    drop: set = set()                 # 0-indexed lines to remove
    insert_after: Dict[int, str] = {}  # 0-indexed line -> feedback to add below it

    for b in blocks:
        shared_text = None
        for ln, t in b["block"]:
            m = SHARED_FEEDBACK_RE.match(t)
            if m:
                shared_text = m.group(1)
                drop.add(ln - 1)
                break
        if shared_text is None:
            continue

        block_lines = b["block"]
        for i, (ln, t) in enumerate(block_lines):
            if not is_answer_line(t):
                continue
            # Does this choice already have its own feedback line?
            has_own = False
            for _ln2, t2 in block_lines[i + 1:]:
                if not t2.strip():
                    continue
                m2 = FEEDBACK_RE.match(t2)
                has_own = bool(m2 and m2.group(1) == "...")
                break
            if not has_own:
                insert_after[ln - 1] = shared_text

    if not drop and not insert_after:
        return text

    print(f"Shared feedback: {len(drop)} \"...*\" line(s) copied onto {len(insert_after)} answer choice(s).")

    out = []
    for i, ln in enumerate(lines):
        if i in drop:
            continue
        out.append(ln)
        if i in insert_after:
            out.append("... " + insert_after[i])
    return "\n".join(out)


def _urlquote(path: str) -> str:
    return urllib.parse.quote(path)


# ---------------- Images ----------------
# Markdown image syntax:  ![alt text](filename.png)  /  ![alt](file.png "title")
# Group 1 = "![alt](" prefix, group 2 = everything inside the parens, group 3 = ")"
# Group 2 is captured whole (not split at whitespace) so file names containing
# spaces, e.g. "Fig 2.png", survive intact.
MD_IMAGE_RE = re.compile(r'(!\[[^\]]*\]\()([^)]*)(\))')

# Optional trailing "title" or 'title' after the image path
MD_IMAGE_TITLE_RE = re.compile(r'\s+(["\'][^"\']*["\'])\s*$')

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"}


def is_image_filename(name: str) -> bool:
    return Path(name).suffix.lower() in IMAGE_EXTS


def _is_remote_src(src: str) -> bool:
    return src.lower().startswith(("http://", "https://", "data:"))


def _find_image_file(ref: str, search_dir: Path):
    """
    Locate an image referenced in the quiz text inside search_dir.
    Tries the reference as given, then just the base name, then the
    sanitized base name, then a case-insensitive match.
    """
    if not search_dir or not search_dir.is_dir():
        return None

    base = Path(ref).name
    candidates = [
        search_dir / ref,
        search_dir / base,
        search_dir / sanitize_filename(base),
    ]
    for c in candidates:
        try:
            if c.is_file():
                return c
        except OSError:
            continue

    wanted = {base.lower(), sanitize_filename(base).lower()}
    for f in search_dir.iterdir():
        if f.is_file() and f.name.lower() in wanted:
            return f
    return None


def indent_standalone_images(text: str) -> str:
    """
    text2qti's line parser rejects a line that starts with "!" ("Missing
    whitespace after !"), because only indented lines continue the element
    above them. Instructors naturally put the picture on its own line under
    the question, so indent those lines automatically.

    Only lines that follow a non-blank line are indented; an image with
    nothing above it has nothing to attach to and is left alone so the normal
    error messages still make sense.
    """
    out: List[str] = []
    prev_nonblank = False
    for line in text.splitlines():
        stripped = line.strip()
        if prev_nonblank and stripped.startswith("![") and not line[:1].isspace():
            out.append("    " + stripped)
        else:
            out.append(line)
        prev_nonblank = bool(stripped)
    return "\n".join(out)


def resolve_local_images(text: str, search_dir: Path) -> Tuple[str, List[str]]:
    """
    text2qti resolves relative image paths against the process's CURRENT WORKING
    DIRECTORY, not the folder the quiz file came from. Since uploads land in a
    temp folder, relative references like ![](diagram.png) would fail with
    'File does not exist'. This rewrites every local image reference to an
    absolute path pointing at the uploaded copy.

    Paths containing spaces are wrapped in <angle brackets>, which is the
    Markdown-standard way to keep the destination from being split at
    whitespace.

    Returns (rewritten_text, list_of_missing_references).
    """
    missing: List[str] = []

    def repl(m: "re.Match") -> str:
        inner = m.group(2).strip()
        if not inner:
            return m.group(0)

        # Split an optional Markdown title off the end, then unwrap <angle brackets>
        title = ""
        if inner.startswith("<") and ">" in inner:
            close = inner.index(">")
            src = inner[1:close].strip()
            title = inner[close + 1:].strip()
        else:
            tm = MD_IMAGE_TITLE_RE.search(inner)
            if tm:
                title = tm.group(1)
                src = inner[:tm.start()].strip()
            else:
                src = inner

        if title:
            title = " " + title

        if not src or _is_remote_src(src):
            return m.group(0)

        found = _find_image_file(src, search_dir)
        if found is None:
            p = Path(src).expanduser()
            if p.is_file():
                found = p
            else:
                missing.append(src)
                return m.group(0)

        abs_path = str(found.resolve())
        if " " in abs_path or "(" in abs_path or ")" in abs_path:
            abs_path = f"<{abs_path}>"
        return f"{m.group(1)}{abs_path}{title}{m.group(3)}"

    return MD_IMAGE_RE.sub(repl, text), missing


def _qti_image_src(zip_path: str) -> str:
    """QTI/Canvas reference for an image stored at zip_path (e.g. images/foo.png)."""
    return "%24IMS-CC-FILEBASE%24/" + "/".join(
        urllib.parse.quote(part) for part in zip_path.split("/")
    )


def markdown_images_to_html(text: str, search_dir: Path, allocate) -> Tuple[str, List[str]]:
    """
    Converts Markdown image references into HTML <img> tags for question types
    that do NOT go through text2qti (currently matching questions, which are
    built by build_matching_item_xml).

    allocate(path: Path, data: bytes) -> str
        Called for each image found. Returns the path the image will occupy
        inside the QTI zip (e.g. "images/routes.png"), which lets the caller
        register the bytes and avoid clobbering an identically named image
        that text2qti already placed in the package.

    Returns (converted_text, missing_references).
    """
    missing: List[str] = []

    def repl(m: "re.Match") -> str:
        inner = m.group(2).strip()
        if not inner:
            return m.group(0)

        alt = re.match(r'!\[([^\]]*)\]\($', m.group(1))
        alt_text = alt.group(1) if alt else ""

        if inner.startswith("<") and ">" in inner:
            src = inner[1:inner.index(">")].strip()
        else:
            tm = MD_IMAGE_TITLE_RE.search(inner)
            src = (inner[:tm.start()] if tm else inner).strip()

        if not src:
            return m.group(0)

        if _is_remote_src(src):
            return f'<img src="{src}" alt="{alt_text}" />'

        found = _find_image_file(src, search_dir)
        if found is None:
            p = Path(src).expanduser()
            found = p if p.is_file() else None
        if found is None:
            missing.append(src)
            return m.group(0)

        zip_path = allocate(found, found.read_bytes())
        return f'<img src="{_qti_image_src(zip_path)}" alt="{alt_text}" />'

    return MD_IMAGE_RE.sub(repl, text), missing


def sanitize_filename(name: str) -> str:
    base = Path(name).name
    base = re.sub(r'[^A-Za-z0-9._ -]+', "_", base).strip()
    return base or "quiz.txt"


def filename_to_quiz_title(name: str) -> str:
    """Convert a filename like 'MA100_Week_2_Exam__1.txt' to 'MA100 Week 2 Exam #1'.

    Rules:
    - Strip extension
    - Replace double-underscore (__) followed by a digit with ' #<digit>'
    - Replace remaining underscores with spaces
    - Collapse multiple spaces
    """
    stem = Path(name).stem
    # Double underscore + digit → ' #digit'  (e.g. __1 → #1)
    title = re.sub(r'__(\d)', r' #\1', stem)
    # Any remaining underscores → space
    title = title.replace('_', ' ')
    # Collapse runs of whitespace
    title = re.sub(r' {2,}', ' ', title).strip()
    return title


def repackage_as_canvas_qti(zip_bytes: bytes, title: str) -> bytes:
    """
    Repackages the text2qti zip into the simple 2-file Canvas QTI format
    (imsmanifest.xml + questions.xml) that Canvas reliably reads the title from.
    """
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zin:
        names = zin.namelist()
        assessment_file = next(
            n for n in names
            if n.endswith(".xml") and "assessment_meta" not in n and n != "imsmanifest.xml"
        )
        assessment_xml = zin.read(assessment_file).decode("utf-8", errors="replace")

        raw_manifest = ""
        if "imsmanifest.xml" in names:
            raw_manifest = zin.read("imsmanifest.xml").decode("utf-8", errors="replace")

        # Any non-XML entry text2qti wrote is media (it stores images under images/).
        # These MUST be carried over or the pictures vanish from the quiz.
        media = {
            n: zin.read(n)
            for n in names
            if not n.endswith("/") and not n.lower().endswith(".xml")
        }

    assessment_xml = re.sub(
        r'(<assessment\b[^>]*\btitle=")[^"]*(")',
        rf'\g<1>{title}\2',
        assessment_xml,
    )

    # --- Rebuild the <resource> entries for images ---------------------------
    # Prefer reusing text2qti's own webcontent resource blocks so the href
    # escaping matches the src attributes it wrote into the question XML.
    image_resources = re.findall(
        r'<resource\b[^>]*?type="webcontent".*?</resource>',
        raw_manifest,
        re.DOTALL,
    )
    image_ids = []
    for res in image_resources:
        m = re.search(r'identifier="([^"]+)"', res)
        if m:
            image_ids.append(m.group(1))

    if media and not image_resources:
        # Fallback: manifest was missing or had no image entries; synthesize them.
        image_resources = []
        image_ids = []
        for i, name in enumerate(sorted(media), 1):
            ident = f"qti_image_{i:04d}"
            href = _urlquote(name)
            image_ids.append(ident)
            image_resources.append(
                f'<resource identifier="{ident}" type="webcontent" href="{href}">\n'
                f'      <file href="{href}"/>\n'
                f'    </resource>'
            )

    image_resources_xml = "".join("\n    " + r for r in image_resources)
    image_deps_xml = "".join(
        f'\n      <dependency identifierref="{i}" />' for i in image_ids
    )

    dep_id = _uuid.uuid4().hex
    manifest = f'''<?xml version="1.0" encoding="UTF-8"?>
<manifest identifier="man000001" xmlns="http://www.imsglobal.org/xsd/imsccv1p1/imscp_v1p1" xmlns:lom="http://ltsc.ieee.org/xsd/imsccv1p1/LOM/resource" xmlns:imsmd="http://www.imsglobal.org/xsd/imsmd_v1p2" xsi:schemaLocation="http://www.imsglobal.org/xsd/imsccv1p1/imscp_v1p1 http://www.imsglobal.org/xsd/imscp_v1p1.xsd http://ltsc.ieee.org/xsd/imsccv1p1/LOM/resource http://www.imsglobal.org/profile/cc/ccv1p1/LOM/ccv1p1_lomresource_v1p0.xsd http://www.imsglobal.org/xsd/imsmd_v1p2 http://www.imsglobal.org/xsd/imsmd_v1p2p2.xsd" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <organizations />
  <metadata>
    <schema>IMS Content</schema>
    <schemaversion>1.2</schemaversion>
    <imsmd:lom>
      <imsmd:general>
        <imsmd:title>
          <imsmd:string>{title}</imsmd:string>
        </imsmd:title>
      </imsmd:general>
    </imsmd:lom>
  </metadata>
  <resources>
    <resource identifier="res000001" type="imsqti_xmlv1p2" href="questions.xml">
      <file href="questions.xml" />
      <dependency identifierref="{dep_id}" />{image_deps_xml}
    </resource>{image_resources_xml}
  </resources>
</manifest>'''

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zout:
        zout.writestr("imsmanifest.xml", manifest.encode("utf-8"))
        zout.writestr("questions.xml", assessment_xml.encode("utf-8"))
        for name, data in media.items():
            zout.writestr(name, data)
    return buf.getvalue()


def normalize_essays(text: str) -> str:
    # text2qti expects "____" (4 underscores) commonly; accept ___ and normalize
    return "\n".join(("____" if ln.strip() == "___" else ln) for ln in text.splitlines())


def make_context(lines: List[str], idx0: int, radius: int = 2) -> str:
    start = max(0, idx0 - radius)
    end = min(len(lines), idx0 + radius + 1)
    out = []
    for i in range(start, end):
        prefix = ">> " if i == idx0 else "   "
        out.append(f"{prefix}{i+1}: {lines[i]}")
    return "\n".join(out)


def split_questions(lines: List[str]) -> List[Dict]:
    blocks: List[Dict] = []
    current = None
    pending_points = None

    for i, line in enumerate(lines):
        mp = POINTS_RE.match(line)
        if mp:
            pending_points = int(mp.group(1))
            continue

        mq = Q_START_RE.match(line)
        if mq:
            if current:
                blocks.append(current)
            current = {
                "qnum": int(mq.group(1)),
                "qline_idx": i,
                "qline_no": i + 1,
                "qtext": mq.group(2),
                "block": [(i + 1, line)],
                "points": pending_points,
            }
            pending_points = None
            continue

        if current:
            current["block"].append((i + 1, line))

    if current:
        blocks.append(current)
    return blocks


def detect_question_type(block: List[Tuple[int, str]]) -> str:
    has_ma = any(MA_OPT_RE.match(t) for _, t in block)
    has_mc = any(MC_OPT_RE.match(t) for _, t in block)
    has_blank = any(t.strip() in ("___", "____") for _, t in block)
    has_fitb = any(FITB_ANS_RE.match(t) for _, t in block)
    has_match = any(MATCH_PAIR_RE.match(t) for _, t in block)

    if has_match:
        return "matching"

    if has_ma:
        return "multiple_answers"

    if has_fitb:
        return "fill_in_blank"

    if has_mc:
        opts = []
        for _, t in block:
            m = MC_OPT_RE.match(t)
            if m:
                opts.append(m.group(2).strip().lower())
        if set(opts) == {"true", "false"} and len(opts) == 2:
            return "true_false"
        return "multiple_choice"

    if has_blank:
        return "essay"

    return "unknown"


def validate_feedback_block(b: Dict, qtype: str) -> List[str]:
    """
    Checks the feedback lines inside one question block. The rules mirror what
    text2qti and Canvas actually accept, so the instructor gets a plain-English
    message here instead of a cryptic converter error later.
    """
    errs: List[str] = []
    seen_answer = False        # have any answer options appeared yet?
    q_general = False          # question already has a "..." line
    q_shared = False           # question already has a "...*" line
    q_correct = False          # question already has a "+" line
    q_incorrect = False        # question already has a "-" line
    answer_has_feedback = False  # the answer directly above already has a "..." line

    for ln, t in b["block"][1:]:
        m_shared = SHARED_FEEDBACK_RE.match(t)
        if m_shared:
            if qtype == "essay":
                errs.append(
                    f"Line {ln}: Essay questions have no answer choices, so \"...*\" has nothing "
                    f"to copy itself to. Use \"...\" for general feedback instead."
                )
            elif seen_answer:
                errs.append(
                    f"Line {ln}: \"...*\" shared feedback must sit directly under the question text, "
                    f"above the answer choices."
                )
            elif q_shared:
                errs.append(
                    f"Line {ln}: This question already has a \"...*\" shared feedback line. "
                    f"Use one per question."
                )
            q_shared = True
            continue

        m_empty = FEEDBACK_EMPTY_RE.match(t)
        if m_empty:
            marker = m_empty.group(1)
            errs.append(
                f"Line {ln}: The feedback marker \"{marker}\" has no text after it. "
                f"Write the feedback on the same line, or delete the line."
            )
            continue

        m = FEEDBACK_RE.match(t)
        if m:
            marker = m.group(1)

            if marker == "...":
                if seen_answer:
                    if qtype == "essay":
                        errs.append(
                            f"Line {ln}: Essay feedback goes directly under the question text, "
                            f"above the ___ line."
                        )
                    elif answer_has_feedback:
                        errs.append(
                            f"Line {ln}: That answer already has a feedback line. "
                            f"Use one \"...\" line per answer."
                        )
                    answer_has_feedback = True
                else:
                    if q_general:
                        errs.append(
                            f"Line {ln}: This question already has general feedback. "
                            f"Use one \"...\" line directly under the question text."
                        )
                    q_general = True
            else:
                label = FEEDBACK_LABELS[marker]
                if qtype == "essay":
                    errs.append(
                        f"Line {ln}: Essay questions do not support \"{marker}\" {label} feedback, "
                        f"because there is no right or wrong answer to score. "
                        f"Use \"...\" for general feedback instead."
                    )
                elif seen_answer:
                    errs.append(
                        f"Line {ln}: \"{marker}\" {label} feedback must sit directly under the "
                        f"question text, above the answer choices."
                    )
                elif (marker == "+" and q_correct) or (marker == "-" and q_incorrect):
                    errs.append(
                        f"Line {ln}: This question already has \"{marker}\" {label} feedback. "
                        f"Use one \"{marker}\" line per question."
                    )
                if marker == "+":
                    q_correct = True
                else:
                    q_incorrect = True
            continue

        if is_answer_line(t):
            seen_answer = True
            answer_has_feedback = False
            continue

        # A line that opens with the feedback marker but matched none of the
        # patterns above would otherwise be dropped without a word
        if t.lstrip(" \t\u00a0").startswith(("...", "\u2026")):
            errs.append(
                f"Line {ln}: This looks like a feedback line, but the converter cannot read it.\n"
                f'Detected: "{t.strip()}"\n'
                f"Fix: write it as \"... your feedback text\" for one answer, or "
                f"\"...* your feedback text\" to put the same text on every answer."
            )

    return errs


def validate_text(text: str) -> List[str]:
    errs: List[str] = []
    lines = text.splitlines()
    blocks = split_questions(lines)

    if not blocks:
        return ["No questions found. Start each question with `1.`, `2.`, etc."]

    # Check for duplicate question numbers
    seen_nums = {}
    for b in blocks:
        n = b["qnum"]
        if n in seen_nums:
            errs.append(
                f"Line {b['qline_no']}: Duplicate question number {n}. "
                f"Questions must be numbered sequentially (1, 2, 3...). "
                f"Question {n} already appeared at line {seen_nums[n]}."
            )
        else:
            seen_nums[n] = b["qline_no"]
    if errs:
        return errs

    # Feedback lines that appear before any question have nothing to attach to
    for i, line in enumerate(lines[: blocks[0]["qline_idx"]]):
        m = (FEEDBACK_RE.match(line) or SHARED_FEEDBACK_RE.match(line)
             or FEEDBACK_EMPTY_RE.match(line))
        if m:
            errs.append(
                f"Line {i + 1}: Feedback found before the first question. "
                f"A feedback line has to sit underneath the question (or the answer) it belongs to.\n"
                f'Detected: "{line.strip()}"'
            )
    if errs:
        return errs

    for b in blocks:
        qline_no = b["qline_no"]
        qline_idx = b["qline_idx"]
        qtext = b["qtext"]
        block = b["block"]
        qtype = detect_question_type(block)

        # Catch inline essay blank placed on same line as question
        # Only flag if the block has NO answer options (MC/MA) — underscores in
        # a multiple-choice stem are cosmetic fill-in blanks, not essay markers.
        _block_has_fitb = any(FITB_ANS_RE.match(t) for _, t in block)
        _block_has_mc = any(MC_OPT_RE.match(t) for _, t in block)
        _block_has_ma = any(MA_OPT_RE.match(t) for _, t in block)
        if not _block_has_fitb and not _block_has_mc and not _block_has_ma and ("___" in qtext or "____" in qtext):
            cleaned = qtext.replace("____", "").replace("___", "").rstrip()
            errs.append("\n".join([
                f"Line {qline_no}: Inline blank detected (___). Move it to its own line for Essay.",
                f'Detected: "{lines[qline_idx].strip()}"',
                "Fix:",
                f"{b['qnum']}. {cleaned}",
                "___",
                "Context:",
                make_context(lines, qline_idx),
            ]))
            continue

        # Feedback lines are checked for every recognized question type
        if qtype != "unknown":
            errs.extend(validate_feedback_block(b, qtype))

        if qtype == "unknown":
            errs.append("\n".join([
                f"Line {qline_no}: Question must specify a response type (MC/TF/MA/Essay/Matching).",
                f'Detected: "{lines[qline_idx].strip()}"',
                "Fix: add a) b) c) (mark one with *) OR [ ] / [*] options OR an essay blank line ___ "
                "OR fill-in-blank answers starting with * OR matching pairs using = Left -> Right",
                "Context:",
                make_context(lines, qline_idx),
            ]))
            continue

        if qtype in ("multiple_choice", "true_false"):
            correct_count = 0
            opt_count = 0
            for _, t in block:
                m = MC_OPT_RE.match(t)
                if m:
                    opt_count += 1
                    if t.strip().lstrip().startswith("*"):
                        correct_count += 1

            if opt_count < 2:
                errs.append(f"Line {qline_no}: Multiple choice needs at least 2 options.")
            if correct_count == 0:
                errs.append(f"Line {qline_no}: No correct answer marked. Add * to exactly one option.")
            if correct_count > 1:
                errs.append(f"Line {qline_no}: Multiple choice must have only ONE correct answer. Use [*] for multiple answers.")

        if qtype == "multiple_answers":
            opts = 0
            correct = 0
            for _, t in block:
                m = MA_OPT_RE.match(t)
                if m:
                    opts += 1
                    if m.group(1) == "*":
                        correct += 1
            if opts < 2:
                errs.append(f"Line {qline_no}: Multiple answers needs at least 2 [ ] options.")
            if correct == 0:
                errs.append(f"Line {qline_no}: No correct options marked. Use [*] for one or more correct options.")

        if qtype == "essay":
            blanks = [t for _, t in block if t.strip() in ("___", "____")]
            if not blanks:
                errs.append(f"Line {qline_no}: Essay must include a standalone line containing ___")

        if qtype == "fill_in_blank":
            answers = [t for _, t in block if FITB_ANS_RE.match(t)]
            if not answers:
                errs.append(
                    f"Line {qline_no}: Fill-in-the-blank must have at least one answer line starting with *.\n"
                    f'Example:\n{b["qnum"]}. The color of the sky is ____.\n* blue\n* Blue'
                )

        if qtype == "matching":
            pairs = [(m.group(1).strip(), m.group(2).strip())
                     for _, t in block if (m := MATCH_PAIR_RE.match(t))]
            if len(pairs) < 2:
                errs.append(
                    f"Line {qline_no}: Matching questions need at least 2 pairs.\n"
                    f"Format each pair as:  = Left side -> Right side"
                )
            left_terms = [p[0] for p in pairs]
            dupes = [t for t in set(left_terms) if left_terms.count(t) > 1]
            if dupes:
                errs.append(
                    f"Line {qline_no}: Matching question has duplicate left-side terms: "
                    + ", ".join(f'"{d}"' for d in dupes)
                )
            for left, right in pairs:
                if not left:
                    errs.append(f"Line {qline_no}: Matching pair has empty left side.")
                if not right:
                    errs.append(f"Line {qline_no}: Matching pair has empty right side.")
                if MD_IMAGE_RE.search(left) or MD_IMAGE_RE.search(right):
                    errs.append(
                        f"Line {qline_no}: Images cannot be used inside matching pairs. "
                        "Canvas shows the terms and the dropdown choices as plain text. "
                        "Move the image to the question text line instead."
                    )

    return errs


# ---------------- Matching QTI XML generation ----------------

def _xml_escape(s: str) -> str:
    return (s.replace("&", "&amp;")
             .replace("<", "&lt;")
             .replace(">", "&gt;")
             .replace('"', "&quot;")
             .replace("'", "&apos;"))


def _feedback_html(text: str) -> str:
    """Wrap a plain feedback line as escaped HTML for a QTI <mattext> block."""
    return _xml_escape(f"<p>{text}</p>")


def build_matching_item_xml(qtext: str, pairs: List[Tuple[str, str]], points: int = 1,
                            feedback: str = None, correct_feedback: str = None,
                            incorrect_feedback: str = None,
                            pair_feedback: List[str] = None) -> str:
    """
    Generate a QTI 1.2 <item> XML block for a Canvas matching question.
    pairs = list of (left_term, right_match) tuples.

    feedback            general comment shown to every student
    correct_feedback    comment shown when every pair is matched correctly
    incorrect_feedback  comment shown when at least one pair is wrong
    pair_feedback       list the same length as pairs; each entry is the
                        comment for that row, or None
    """
    pair_feedback = list(pair_feedback or [])
    pair_feedback += [None] * (len(pairs) - len(pair_feedback))
    item_id = "match_" + _uuid.uuid4().hex[:12]

    # Build response identifiers for each pair
    # response_lid per left-term; choices pool from right-side answers
    # Canvas matching: one <response_lid> per left-term, choices are the right-side pool

    right_vals = [p[1] for p in pairs]
    # Assign a stable id to each unique right-side value
    right_ids: Dict[str, str] = {}
    for rv in right_vals:
        if rv not in right_ids:
            right_ids[rv] = "r_" + _uuid.uuid4().hex[:8]

    # Build <respcondition> correct-answer mapping
    # Each left-term gets a <response_lid ident="lN">
    left_ids = []
    for i, (left, _) in enumerate(pairs):
        left_ids.append(f"l_{i}_{_uuid.uuid4().hex[:6]}")

    # --- presentation block ---
    presentation_lines = []
    presentation_lines.append('    <presentation>')
    presentation_lines.append(f'      <material><mattext texttype="text/html">{_xml_escape(qtext)}</mattext></material>')

    for i, (left, right) in enumerate(pairs):
        lid = left_ids[i]
        presentation_lines.append(f'      <response_lid ident="{lid}" rcardinality="Single">')
        presentation_lines.append(f'        <material><mattext texttype="text/plain">{_xml_escape(left)}</mattext></material>')
        presentation_lines.append('        <render_choice>')
        # All right-side options appear as choices for every left-term
        for rv, rid in right_ids.items():
            presentation_lines.append(f'          <response_label ident="{rid}">')
            presentation_lines.append(f'            <material><mattext texttype="text/plain">{_xml_escape(rv)}</mattext></material>')
            presentation_lines.append('          </response_label>')
        presentation_lines.append('        </render_choice>')
        presentation_lines.append('      </response_lid>')

    presentation_lines.append('    </presentation>')

    # --- resprocessing block ---
    resprocessing_lines = []
    resprocessing_lines.append('    <resprocessing>')
    resprocessing_lines.append('      <outcomes>')
    resprocessing_lines.append(f'        <decvar maxvalue="{points}" minvalue="0" varname="SCORE" vartype="Decimal"/>')
    resprocessing_lines.append('      </outcomes>')

    # General feedback fires no matter what the student answers
    if feedback:
        resprocessing_lines.append('      <respcondition continue="Yes">')
        resprocessing_lines.append('        <conditionvar>')
        resprocessing_lines.append('          <other/>')
        resprocessing_lines.append('        </conditionvar>')
        resprocessing_lines.append('        <displayfeedback feedbacktype="Response" linkrefid="general_fb"/>')
        resprocessing_lines.append('      </respcondition>')

    # One respcondition per pair: award (1/N * points) for each correct match
    per_pair_score = round(points / len(pairs), 4)
    for i, (left, right) in enumerate(pairs):
        lid = left_ids[i]
        rid = right_ids[right]
        resprocessing_lines.append('      <respcondition continue="Yes">')
        resprocessing_lines.append('        <conditionvar>')
        resprocessing_lines.append(f'          <varequal respident="{lid}">{rid}</varequal>')
        resprocessing_lines.append('        </conditionvar>')
        resprocessing_lines.append(f'        <setvar action="Add" varname="SCORE">{per_pair_score}</setvar>')
        if pair_feedback[i]:
            resprocessing_lines.append(f'        <displayfeedback feedbacktype="Response" linkrefid="{lid}_fb"/>')
        resprocessing_lines.append('      </respcondition>')

    # All pairs right / at least one wrong
    if correct_feedback or incorrect_feedback:
        all_correct = "\n".join(
            f'            <varequal respident="{left_ids[i]}">{right_ids[right]}</varequal>'
            for i, (_left, right) in enumerate(pairs)
        )
        if correct_feedback:
            resprocessing_lines.append('      <respcondition continue="Yes">')
            resprocessing_lines.append('        <conditionvar>')
            resprocessing_lines.append('          <and>')
            resprocessing_lines.append(all_correct)
            resprocessing_lines.append('          </and>')
            resprocessing_lines.append('        </conditionvar>')
            resprocessing_lines.append('        <displayfeedback feedbacktype="Response" linkrefid="correct_fb"/>')
            resprocessing_lines.append('      </respcondition>')
        if incorrect_feedback:
            resprocessing_lines.append('      <respcondition continue="Yes">')
            resprocessing_lines.append('        <conditionvar>')
            resprocessing_lines.append('          <not>')
            resprocessing_lines.append('            <and>')
            resprocessing_lines.append(all_correct)
            resprocessing_lines.append('            </and>')
            resprocessing_lines.append('          </not>')
            resprocessing_lines.append('        </conditionvar>')
            resprocessing_lines.append('        <displayfeedback feedbacktype="Response" linkrefid="general_incorrect_fb"/>')
            resprocessing_lines.append('      </respcondition>')

    resprocessing_lines.append('    </resprocessing>')

    # --- itemfeedback blocks ---
    feedback_lines = []

    def _add_itemfeedback(ident: str, text: str):
        feedback_lines.append(f'    <itemfeedback ident="{ident}">')
        feedback_lines.append('      <flow_mat>')
        feedback_lines.append('        <material>')
        feedback_lines.append(f'          <mattext texttype="text/html">{_feedback_html(text)}</mattext>')
        feedback_lines.append('        </material>')
        feedback_lines.append('      </flow_mat>')
        feedback_lines.append('    </itemfeedback>')

    if feedback:
        _add_itemfeedback("general_fb", feedback)
    if correct_feedback:
        _add_itemfeedback("correct_fb", correct_feedback)
    if incorrect_feedback:
        _add_itemfeedback("general_incorrect_fb", incorrect_feedback)
    for i, fb_text in enumerate(pair_feedback):
        if fb_text:
            _add_itemfeedback(f"{left_ids[i]}_fb", fb_text)

    # --- itemmetadata ---
    meta = f'''    <itemmetadata>
      <qtimetadata>
        <qtimetadatafield>
          <fieldlabel>question_type</fieldlabel>
          <fieldentry>matching_question</fieldentry>
        </qtimetadatafield>
        <qtimetadatafield>
          <fieldlabel>points_possible</fieldlabel>
          <fieldentry>{points}</fieldentry>
        </qtimetadatafield>
      </qtimetadata>
    </itemmetadata>'''

    item_xml = f'  <item ident="{item_id}" title="Matching Question">\n'
    item_xml += meta + "\n"
    item_xml += "\n".join(presentation_lines) + "\n"
    item_xml += "\n".join(resprocessing_lines) + "\n"
    if feedback_lines:
        item_xml += "\n".join(feedback_lines) + "\n"
    item_xml += "  </item>"
    return item_xml


# ---------------- Matching pre/post processing ----------------

def extract_matching_blocks(text: str) -> Tuple[str, List[Dict]]:
    """
    Scans the quiz text for matching questions (detected by = X -> Y pairs).
    Returns:
      - text_without_matching: the original text with matching question blocks
        replaced by placeholder comments (so text2qti won't choke on them)
      - matching_blocks: list of dicts with keys: qnum, qtext, pairs, points
    """
    lines = text.splitlines()
    blocks = split_questions(lines)

    matching_blocks = []
    # Track which line ranges to blank out
    remove_ranges: List[Tuple[int, int]] = []  # (start_line_no, end_line_no) 1-indexed

    for b in blocks:
        qtype = detect_question_type(b["block"])
        if qtype != "matching":
            continue

        pairs = []
        pair_feedback: List[str] = []   # one slot per pair, None when unused
        image_lines = []
        general_fb = None
        correct_fb = None
        incorrect_fb = None

        for _, t in b["block"]:
            m = MATCH_PAIR_RE.match(t)
            if m:
                pairs.append((m.group(1).strip(), m.group(2).strip()))
                pair_feedback.append(None)
                continue

            fb = FEEDBACK_RE.match(t)
            if fb:
                marker, fb_text = fb.group(1), fb.group(2)
                if marker == "...":
                    # After a pair it belongs to that pair; before the pairs it
                    # is general feedback for the whole question.
                    if pairs:
                        pair_feedback[-1] = fb_text
                    else:
                        general_fb = fb_text
                elif marker == "+":
                    correct_fb = fb_text
                else:
                    incorrect_fb = fb_text
                continue

            if t.strip().startswith("!["):
                # Image sitting on its own line under the matching stem. Only
                # the first line of a question reaches qtext, so pull it in
                # here or the picture would be silently dropped.
                image_lines.append(t.strip())

        qtext = b["qtext"]
        if image_lines:
            qtext = qtext + "\n" + "\n".join(image_lines)

        matching_blocks.append({
            "qnum": b["qnum"],
            "qtext": qtext,
            "pairs": pairs,
            "points": b["points"] or 1,
            "feedback": general_fb,
            "correct_feedback": correct_fb,
            "incorrect_feedback": incorrect_fb,
            "pair_feedback": pair_feedback,
        })

        # Determine line range of this block
        line_nos = [ln for ln, _ in b["block"]]
        # Also include any "Points: X" line immediately before
        start = min(line_nos)
        end = max(line_nos)
        # Check if there's a Points: line just before
        if start >= 2 and POINTS_RE.match(lines[start - 2]):  # lines is 0-indexed, start is 1-indexed
            start -= 1
        remove_ranges.append((start, end))

    if not matching_blocks:
        return text, []

    # Build cleaned text (replace matching blocks with blank lines to keep numbering intent)
    new_lines = list(lines)
    for start_no, end_no in remove_ranges:
        for i in range(start_no - 1, end_no):
            new_lines[i] = ""

    cleaned = "\n".join(new_lines)
    return cleaned, matching_blocks


def inject_matching_into_zip(zip_bytes: bytes, matching_blocks: List[Dict], non_matching_qnums: List[int],
                             image_dir: Path = None) -> bytes:
    """
    Opens the QTI zip, parses existing <item> elements (produced by text2qti for
    non-matching questions), then reassembles ALL items in the correct original
    question-number order by interleaving matching items at their proper positions.

    non_matching_qnums: ordered list of question numbers that text2qti processed,
                        in the same order text2qti emitted them.
    matching_blocks:    list of dicts with keys qnum, qtext, pairs, points.
    image_dir:          folder holding uploaded images referenced by matching
                        question text. Matching items are built here rather than
                        by text2qti, so their images must be bundled here too.
    """
    if not matching_blocks:
        return zip_bytes

    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zin:
        names = zin.namelist()
        files = {n: zin.read(n) for n in names}

    # --- Images used by matching questions --------------------------------
    # text2qti may already have written images/ entries for the other question
    # types. Reuse an entry when the bytes are identical; otherwise pick a
    # non-colliding name so neither picture is lost.
    new_images: Dict[str, bytes] = {}

    def allocate(path: Path, data: bytes) -> str:
        base = sanitize_filename(path.name)
        candidate = f"images/{base}"
        stem, suffix = Path(base).stem, Path(base).suffix
        i = 2
        while True:
            existing = files.get(candidate, new_images.get(candidate))
            if existing is None or existing == data:
                new_images[candidate] = data
                return candidate
            candidate = f"images/{stem}_{i}{suffix}"
            i += 1

    search_dir = Path(image_dir) if image_dir else None
    for mb in matching_blocks:
        if "![" in mb["qtext"]:
            converted, _missing = markdown_images_to_html(mb["qtext"], search_dir, allocate)
            # An image the instructor put on its own line should render on its
            # own line; a bare newline would collapse to a space in HTML.
            converted = converted.replace("\n<img", "<br /><img")
            mb["qtext"] = converted

    questions_xml_name = "questions.xml"
    if questions_xml_name not in files:
        for n in names:
            if n.endswith(".xml") and n != "imsmanifest.xml":
                questions_xml_name = n
                break

    questions_xml = files[questions_xml_name].decode("utf-8", errors="replace")

    # --- Extract existing <item>...</item> blocks from text2qti output ---
    item_pattern = re.compile(r'(<item\b[^>]*>.*?</item>)', re.DOTALL)
    existing_items = item_pattern.findall(questions_xml)

    # Map question number -> item XML for non-matching questions
    non_matching_map: Dict[int, str] = {}
    for qnum, item_xml in zip(non_matching_qnums, existing_items):
        non_matching_map[qnum] = item_xml

    # Map question number -> matching item XML
    matching_map: Dict[int, str] = {
        mb["qnum"]: build_matching_item_xml(
            mb["qtext"],
            mb["pairs"],
            mb["points"],
            feedback=mb.get("feedback"),
            correct_feedback=mb.get("correct_feedback"),
            incorrect_feedback=mb.get("incorrect_feedback"),
            pair_feedback=mb.get("pair_feedback"),
        )
        for mb in matching_blocks
    }

    # Build full ordered list of all question numbers
    all_qnums = sorted(set(non_matching_map.keys()) | set(matching_map.keys()))

    # Assemble items in order
    ordered_items = []
    for qnum in all_qnums:
        if qnum in matching_map:
            ordered_items.append(matching_map[qnum])
        elif qnum in non_matching_map:
            ordered_items.append(non_matching_map[qnum])

    items_xml = "\n".join(ordered_items)

    # Replace the entire <section>...</section> content with the reordered items
    section_pattern = re.compile(r'(<section\b[^>]*>)(.*?)(</section>)', re.DOTALL)
    if section_pattern.search(questions_xml):
        questions_xml = section_pattern.sub(
            lambda m: m.group(1) + "\n" + items_xml + "\n  " + m.group(3),
            questions_xml,
            count=1,
        )
    elif "</assessment>" in questions_xml:
        section_wrap = f'  <section ident="root_section">\n{items_xml}\n  </section>\n'
        questions_xml = questions_xml.replace("</assessment>", section_wrap + "</assessment>", 1)
    else:
        raise RuntimeError("Could not find insertion point in QTI XML for matching questions.")

    files[questions_xml_name] = questions_xml.encode("utf-8")

    # --- Register any newly added matching images in the manifest ----------
    if new_images and "imsmanifest.xml" in files:
        manifest = files["imsmanifest.xml"].decode("utf-8", errors="replace")
        added_resources = []
        added_deps = []
        for i, (zip_path, _data) in enumerate(sorted(new_images.items()), 1):
            href = _urlquote(zip_path)
            if f'href="{href}"' in manifest:
                continue  # already declared (image shared with a text2qti question)
            ident = f"match_image_{i:04d}_{_uuid.uuid4().hex[:8]}"
            added_deps.append(f'      <dependency identifierref="{ident}" />')
            added_resources.append(
                f'    <resource identifier="{ident}" type="webcontent" href="{href}">\n'
                f'      <file href="{href}"/>\n'
                f'    </resource>'
            )

        if added_resources:
            manifest = manifest.replace(
                '<file href="questions.xml" />',
                '<file href="questions.xml" />\n' + "\n".join(added_deps),
                1,
            )
            manifest = manifest.replace(
                "  </resources>",
                "\n".join(added_resources) + "\n  </resources>",
                1,
            )
            files["imsmanifest.xml"] = manifest.encode("utf-8")

    for zip_path, data in new_images.items():
        files.setdefault(zip_path, data)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zout:
        for name, data in files.items():
            zout.writestr(name, data)
    return buf.getvalue()


def has_non_matching_questions(text: str) -> bool:
    """Returns True if the text (after stripping matching blocks) has any real content for text2qti."""
    stripped = text.strip()
    # Remove blank lines
    non_blank = [l for l in stripped.splitlines() if l.strip()]
    return bool(non_blank)


def build_empty_qti_zip(title: str) -> bytes:
    """
    Builds a minimal valid Canvas QTI zip with an empty question bank.
    Used when the quiz contains ONLY matching questions (text2qti has nothing to process).
    """
    dep_id = _uuid.uuid4().hex
    assess_id = "assess_" + _uuid.uuid4().hex[:12]

    questions_xml = f'''<?xml version="1.0" encoding="UTF-8"?>
<questestinterop xmlns="http://www.imsglobal.org/xsd/ims_qtiasiv1p2"
  xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
  xsi:schemaLocation="http://www.imsglobal.org/xsd/ims_qtiasiv1p2 http://www.imsglobal.org/xsd/ims_qtiasiv1p2p1.xsd">
  <assessment ident="{assess_id}" title="{_xml_escape(title)}">
    <qtimetadata>
      <qtimetadatafield>
        <fieldlabel>cc_maxattempts</fieldlabel>
        <fieldentry>1</fieldentry>
      </qtimetadatafield>
    </qtimetadata>
    <section ident="root_section">
    </section>
  </assessment>
</questestinterop>'''

    manifest = f'''<?xml version="1.0" encoding="UTF-8"?>
<manifest identifier="man000001" xmlns="http://www.imsglobal.org/xsd/imsccv1p1/imscp_v1p1"
  xmlns:lom="http://ltsc.ieee.org/xsd/imsccv1p1/LOM/resource"
  xmlns:imsmd="http://www.imsglobal.org/xsd/imsmd_v1p2"
  xsi:schemaLocation="http://www.imsglobal.org/xsd/imsccv1p1/imscp_v1p1 http://www.imsglobal.org/xsd/imscp_v1p1.xsd"
  xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <organizations />
  <metadata>
    <schema>IMS Content</schema>
    <schemaversion>1.2</schemaversion>
    <imsmd:lom>
      <imsmd:general>
        <imsmd:title><imsmd:string>{_xml_escape(title)}</imsmd:string></imsmd:title>
      </imsmd:general>
    </imsmd:lom>
  </metadata>
  <resources>
    <resource identifier="res000001" type="imsqti_xmlv1p2" href="questions.xml">
      <file href="questions.xml" />
      <dependency identifierref="{dep_id}" />
    </resource>
  </resources>
</manifest>'''

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zout:
        zout.writestr("imsmanifest.xml", manifest.encode("utf-8"))
        zout.writestr("questions.xml", questions_xml.encode("utf-8"))
    return buf.getvalue()


def text2qti_status() -> str:
    if TEXT2QTI_IMPORT_ERROR:
        return (
            "text2qti Python package is not available: " + TEXT2QTI_IMPORT_ERROR + "\n"
            "Fix: open a terminal/command prompt and run:  pip install text2qti"
        )
    return "text2qti library loaded successfully (running in-process, no external exe needed)."


def run_text2qti_to_bytes(src: Path, original_name: str = "", image_dir: Path = None) -> tuple[bytes, str]:
    """
    Runs text2qti and returns (zip_bytes, zip_filename).
    Critical: bytes are read BEFORE TemporaryDirectory is cleaned up.

    image_dir: folder holding any uploaded image files referenced by the quiz
               text. Defaults to the folder the quiz file itself is in.
    """
    quiz_title = Path(original_name).stem if original_name else filename_to_quiz_title(src.name)
    image_dir = Path(image_dir) if image_dir else src.parent

    with TemporaryDirectory() as td:
        td = Path(td)

        work_src = td / sanitize_filename(src.name)
        txt = src.read_text(encoding="utf-8", errors="ignore")
        txt = normalize_essays(txt)
        txt = normalize_feedback_markers(txt)
        txt = indent_standalone_images(txt)

        # --- Validate full text first (including matching) ---
        val_errs = validate_text(txt)
        if val_errs:
            raise ValidationError("\n".join(val_errs[:10]))

        # --- Copy any "...*" shared feedback onto each answer choice ---
        txt = expand_shared_choice_feedback(txt)

        # --- Point local image references at the uploaded files ---
        txt, missing_images = resolve_local_images(txt, image_dir)
        if missing_images:
            names = ", ".join(sorted(set(missing_images)))
            raise ValidationError(
                f"Image file(s) not found: {names}\n"
                "Fix: select the image files in the same upload as the quiz .txt file "
                "(hold Ctrl/Cmd and pick the .txt and the images together), and make sure "
                "the file names in the quiz text match exactly, including the extension.\n"
                "Alternatively, use a full web address: ![alt](https://example.com/image.png)"
            )

        # --- Extract matching questions; get cleaned text for text2qti ---
        cleaned_txt, matching_blocks = extract_matching_blocks(txt)

        # Collect the question numbers for non-matching questions, in order
        lines = txt.splitlines()
        all_blocks = split_questions(lines)
        matching_qnums = {mb["qnum"] for mb in matching_blocks}
        non_matching_qnums = [b["qnum"] for b in all_blocks if b["qnum"] not in matching_qnums]

        if has_non_matching_questions(cleaned_txt):
            # Normal path: convert non-matching questions using text2qti's
            # Python API directly (in-process). No external .exe, no PATH
            # lookup, no per-machine install location required.
            if TEXT2QTI_IMPORT_ERROR:
                raise RuntimeError(
                    "text2qti Python package is not installed in this environment.\n"
                    f"Import error: {TEXT2QTI_IMPORT_ERROR}\n"
                    "Fix: open a terminal/command prompt and run:  pip install text2qti"
                )

            try:
                config = _T2QConfig()
                config.load()
                quiz_obj = _T2QQuiz(cleaned_txt, config=config, source_name=work_src.as_posix())
                qti_obj = _T2QQTI(quiz_obj)
                raw_bytes = qti_obj.zip_bytes()
            except Text2qtiError as e:
                raise RuntimeError(f"text2qti could not convert this file:\n{e}")

            patched_bytes = repackage_as_canvas_qti(raw_bytes, quiz_title)

        else:
            # Matching-only quiz: skip text2qti entirely
            print("All questions are matching type — building QTI zip directly.")
            patched_bytes = build_empty_qti_zip(quiz_title)
            non_matching_qnums = []

        # Inject matching questions into the zip in correct order
        final_bytes = inject_matching_into_zip(patched_bytes, matching_blocks, non_matching_qnums,
                                               image_dir=image_dir)
        return final_bytes, quiz_title + ".zip"


# ---------------- Routes ----------------

@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "GET":
        return render_template_string(HTML)

    print("=== POST / received ===")

    uploaded_files = request.files.getlist("files")
    uploaded_files = [f for f in uploaded_files if f and f.filename]

    if not uploaded_files:
        return jsonify({"reason": "No file uploaded."}), 400

    # Images can be uploaded alongside the quiz text files; they are saved next
    # to the quiz files so ![](diagram.png) references can be resolved.
    image_uploads = [f for f in uploaded_files if is_image_filename(f.filename)]
    quiz_uploads  = [f for f in uploaded_files if not is_image_filename(f.filename)]

    if not quiz_uploads:
        return jsonify({
            "reason": "Only image files were uploaded. Include at least one quiz .txt file."
        }), 400

    # Convert every uploaded file; collect results and errors
    results = []   # list of (zip_name, zip_bytes)
    errors  = []   # list of "filename: reason" strings

    with TemporaryDirectory() as td:
        td = Path(td)

        for img in image_uploads:
            img_path = td / sanitize_filename(img.filename)
            img.save(str(img_path))
            print(f"Saved image: {img_path.name}")

        for uploaded in quiz_uploads:
            print(f"Processing: {uploaded.filename}")
            safe_name = sanitize_filename(uploaded.filename)
            in_path = td / safe_name
            uploaded.save(str(in_path))
            try:
                zip_bytes, zip_name = run_text2qti_to_bytes(in_path, uploaded.filename, image_dir=td)
                results.append((zip_name, zip_bytes))
                print(f"  OK: {zip_name} ({len(zip_bytes)} bytes)")
            except ValidationError as ve:
                msg = str(ve).strip()
                errors.append(f"{uploaded.filename}:\n{msg}")
                print(f"  Validation error: {msg}")
            except RuntimeError as re_err:
                # RuntimeErrors are raised intentionally with a clear, safe-to-show
                # message (e.g. "text2qti.exe missing: ...", "no .zip was produced").
                # Surface them directly instead of hiding them behind a generic message.
                msg = str(re_err).strip()
                tb = traceback.format_exc()
                print(f"  Runtime error:\n{tb}")
                errors.append(f"{uploaded.filename}:\n{msg}")
            except Exception:
                tb = traceback.format_exc()
                print(f"  Error:\n{tb}")
                errors.append(f"{uploaded.filename}: Conversion failed. Please check formatting.")

    # Nothing converted successfully
    if not results:
        reason = "\n\n".join(errors) if errors else "No files could be converted."
        return jsonify({"reason": reason}), 400

    # Single file with no errors — return it directly (original behaviour)
    if len(results) == 1 and not errors:
        zip_name, zip_bytes = results[0]
        resp = Response(zip_bytes, mimetype="application/zip")
        resp.headers["Content-Disposition"] = f"attachment; filename={zip_name}"
        return resp

    # Multiple files (or mix of success/failure) — bundle all into one zip
    bundle_buf = io.BytesIO()
    with zipfile.ZipFile(bundle_buf, "w", compression=zipfile.ZIP_DEFLATED) as zout:
        for zip_name, zip_bytes in results:
            zout.writestr(zip_name, zip_bytes)
        if errors:
            zout.writestr("ERRORS.txt", "\n\n".join(errors))
    bundle_buf.seek(0)

    resp = Response(bundle_buf.read(), mimetype="application/zip")
    resp.headers["Content-Disposition"] = "attachment; filename=QTI_Bundle.zip"
    return resp


def open_browser():
    time.sleep(1)
    try:
        webbrowser.open(f"http://{HOST}:{PORT}")
    except Exception:
        pass


if __name__ == "__main__":
    print(f"Python: {sys.executable}")
    print(text2qti_status())
    if not IS_RENDER:
        threading.Thread(target=open_browser, daemon=True).start()
    print(f"Serving on http://{'localhost' if not IS_RENDER else HOST}:{PORT}")
    app.run(host=HOST, port=PORT, debug=False)
