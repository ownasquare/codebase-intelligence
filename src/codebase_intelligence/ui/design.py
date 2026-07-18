"""Neutral visual contract for the repository workbench."""

APP_STYLES = """
<style>
  :root {
    --ci-ink: #172033;
    --ci-muted: #647084;
    --ci-line: #d9dee7;
    --ci-line-strong: #bcc5d2;
    --ci-canvas: #f6f7f9;
    --ci-surface: #ffffff;
    --ci-subtle: #eef2f5;
    --ci-action: #0f766e;
    --ci-action-hover: #115e59;
    --ci-focus: #2563eb;
    --ci-danger: #b42318;
  }

  [data-testid="stAppViewContainer"] {
    background: var(--ci-canvas);
    color: var(--ci-ink);
  }
  [data-testid="stHeader"] { background: var(--ci-canvas); }
  [data-testid="stMainBlockContainer"] {
    max-width: 1160px;
    padding-top: 3.75rem;
    padding-bottom: 4rem;
  }
  [data-testid="stSidebar"] {
    background: var(--ci-surface);
    border-right: 1px solid var(--ci-line);
  }
  [data-testid="stSidebarContent"] { padding-top: 1rem; }

  h1, h2, h3, h4 { color: var(--ci-ink); letter-spacing: -0.015em; }
  h1 { font-size: 1.55rem !important; line-height: 1.25 !important; }
  h2 { font-size: 1.2rem !important; }
  h3 { font-size: 1rem !important; }
  p, label, [data-testid="stCaptionContainer"] { color: var(--ci-muted); }

  [data-testid="stVerticalBlockBorderWrapper"] {
    background: var(--ci-surface);
    border-color: var(--ci-line);
    border-radius: 9px;
    box-shadow: none;
  }
  [data-testid="stMetric"] {
    background: var(--ci-surface);
    border: 1px solid var(--ci-line);
    border-radius: 8px;
    padding: 0.65rem 0.75rem;
  }
  [data-testid="stMetricLabel"] { color: var(--ci-muted); }
  [data-testid="stMetricValue"] { color: var(--ci-ink); font-size: 1.35rem; }

  [data-testid="stRadio"] [role="radiogroup"] {
    background: var(--ci-subtle);
    border: 1px solid var(--ci-line);
    border-radius: 8px;
    gap: 0.25rem;
    padding: 0.2rem;
  }
  [data-testid="stRadio"] label {
    min-height: 40px;
    align-items: center;
    border-radius: 6px;
    padding: 0.15rem 0.7rem;
  }

  div.stButton > button,
  div.stFormSubmitButton > button,
  div.stDownloadButton > button,
  [data-testid="stLinkButton"] a {
    min-height: 44px;
    border-radius: 7px;
    border-color: var(--ci-line-strong);
    box-shadow: none;
    font-weight: 600;
  }
  div.stButton > button[kind^="primary"],
  div.stFormSubmitButton > button[kind^="primary"] {
    background: var(--ci-action);
    border-color: var(--ci-action);
  }
  div.stButton > button[kind^="primary"] p,
  div.stFormSubmitButton > button[kind^="primary"] p {
    color: #ffffff;
  }
  div.stButton > button[kind^="primary"]:hover,
  div.stFormSubmitButton > button[kind^="primary"]:hover {
    background: var(--ci-action-hover);
    border-color: var(--ci-action-hover);
  }
  button:focus-visible,
  input:focus-visible,
  textarea:focus-visible,
  [role="radio"]:focus-visible,
  a:focus-visible {
    outline: 3px solid var(--ci-focus) !important;
    outline-offset: 2px;
  }

  [data-testid="stTextInputRootElement"],
  [data-testid="stTextAreaRootElement"],
  [data-baseweb="select"] > div {
    border-radius: 7px;
  }
  [data-testid="stExpander"] {
    background: var(--ci-surface);
    border-color: var(--ci-line);
    border-radius: 8px;
    box-shadow: none;
  }
  [data-testid="stFileUploaderDropzone"] {
    background: var(--ci-subtle);
    border-color: var(--ci-line-strong);
  }
  [data-testid="stCode"] {
    border: 1px solid var(--ci-line);
    border-radius: 7px;
    overflow-x: auto;
  }
  [data-testid="stCode"] pre {
    overflow-x: auto;
    white-space: pre;
  }
  code { overflow-wrap: anywhere; }

  hr { border-color: var(--ci-line) !important; }

  @media (max-width: 760px) {
    [data-testid="stMainBlockContainer"] { padding: 3.5rem 0.75rem 4rem; }
    [data-testid="stHorizontalBlock"] { gap: 0.55rem; }
    [data-testid="stRadio"] [role="radiogroup"] { flex-wrap: wrap; }
    [data-testid="stRadio"] label { min-width: calc(50% - 0.25rem); }
    [data-testid="stMetric"] { padding: 0.55rem 0.6rem; }
    h1 { font-size: 1.35rem !important; }
  }
</style>
"""

__all__ = ["APP_STYLES"]
