"""
Web GUI
"""

from __future__ import annotations

import argparse
import gzip
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import socket
import threading
from typing import Any, Dict, List
import urllib.parse
import webbrowser

# Import core normalization and generation logic
from generate_mal_xml import (
    MIN_REQUEST_INTERVAL,
    build_mal_xml,
    build_mal_manga_xml,
    deduplicate_entries,
    resolve_anime_entries,
    resolve_manga_entries,
    sanitize_title_line,
    set_rate_limit_notifier,
)

BASE_DIR = Path(__file__).resolve().parent
EXPORT_MEDIA_TYPE = "anime"

# Automatically detect output/ directory
if (BASE_DIR / "output").is_dir():
    EXPORT_XML_PATH = BASE_DIR / "output" / "myanimelist_export.xml"
    EXPORT_GZ_PATH = BASE_DIR / "output" / "myanimelist_export.xml.gz"
else:
    EXPORT_XML_PATH = BASE_DIR / "myanimelist_export.xml"
    EXPORT_GZ_PATH = BASE_DIR / "myanimelist_export.xml.gz"

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>AniExport &mdash; Universal XML Generator for MAL &amp; AniList</title>
  <link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'%3E%3Crect width='24' height='24' rx='5' fill='%233FA796'/%3E%3Cpath d='M12 4 4 9l8 5 8-5-8-5zm-8 8 8 5 8-5' fill='none' stroke='white' stroke-width='2' stroke-linejoin='round'/%3E%3C/svg%3E">
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Figtree:wght@400;500;600;700&family=Fira+Code:wght@400;500&display=swap" rel="stylesheet">
  <style>
    :root {
      --bg: #0C0E12;
      --surface-1: #12161B;
      --surface-2: #191E24;
      --surface-3: #22282F;
      --border: rgba(170, 180, 190, 0.10);
      --border-hover: rgba(170, 180, 190, 0.22);
      --border-focus: var(--accent);
      --text: #EAE8E4;
      --text-muted: #9CA3AC;
      --text-subtle: #6E757F;

      --accent: #3FA796;
      --accent-hover: #348D7E;
      --accent-active: #2B766A;
      --accent-text: #7CC7B9;
      --accent-bg: rgba(63, 167, 150, 0.14);
      --accent-tint: rgba(63, 167, 150, 0.30);

      --success: #82B489;
      --success-bg: rgba(130, 180, 137, 0.12);
      --warning: #C9A55C;
      --warning-bg: rgba(201, 165, 92, 0.12);
      --danger: #C96F62;
      --danger-bg: rgba(201, 111, 98, 0.12);
      --planned-text: #9AA4B2;
      --planned-bg: rgba(154, 164, 178, 0.12);
      --paused-text: #A8ADB5;
      --paused-bg: rgba(168, 173, 181, 0.12);

      --panel-tint: rgba(18, 22, 27, 0.6);
      --table-tint: rgba(18, 22, 27, 0.4);
      --row-hover: rgba(170, 180, 190, 0.08);
      --row-excluded-bg: rgba(18, 22, 27, 0.35);
      --card-tint: rgba(25, 30, 36, 0.5);

      --font-sans: 'Figtree', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
      --font-mono: 'Fira Code', ui-monospace, SFMono-Regular, monospace;
      --radius-sm: 6px;
      --radius-md: 10px;
      --radius-lg: 14px;
      --shadow-sm: 0 1px 2px 0 rgba(6, 8, 12, 0.40);
      --shadow-md: 0 4px 14px 0 rgba(6, 8, 12, 0.45);
      --shadow-lg: 0 12px 30px 0 rgba(6, 8, 12, 0.55);
    }

    body.light {
      --bg: #EFEBE4;
      --surface-1: #F7F5F0;
      --surface-2: #FFFFFF;
      --surface-3: #E9E5DD;
      --border: rgba(40, 44, 50, 0.10);
      --border-hover: rgba(40, 44, 50, 0.22);
      --text: #1E2126;
      --text-muted: #5A616B;
      --text-subtle: #8A919B;

      --accent: #2E8577;
      --accent-hover: #276F63;
      --accent-active: #1F5B50;
      --accent-text: #23705F;
      --accent-bg: rgba(46, 133, 119, 0.10);
      --accent-tint: rgba(46, 133, 119, 0.24);

      --success: #43764A;
      --success-bg: rgba(67, 118, 74, 0.10);
      --warning: #97742B;
      --warning-bg: rgba(151, 116, 43, 0.10);
      --danger: #A85246;
      --danger-bg: rgba(168, 82, 70, 0.10);
      --planned-text: #5D6572;
      --planned-bg: rgba(93, 101, 114, 0.10);
      --paused-text: #6B727C;
      --paused-bg: rgba(107, 114, 124, 0.12);

      --panel-tint: rgba(247, 245, 240, 0.75);
      --table-tint: rgba(247, 245, 240, 0.6);
      --row-hover: rgba(40, 44, 50, 0.05);
      --row-excluded-bg: rgba(40, 44, 50, 0.05);
      --card-tint: rgba(255, 255, 255, 0.55);

      --shadow-sm: 0 1px 2px 0 rgba(60, 56, 48, 0.10);
      --shadow-md: 0 4px 14px 0 rgba(60, 56, 48, 0.10);
      --shadow-lg: 0 12px 30px 0 rgba(60, 56, 48, 0.14);
    }

    * {
      box-sizing: border-box;
      margin: 0;
      padding: 0;
    }

    ::selection {
      background: var(--accent);
      color: #FFFFFF;
    }

    ::-webkit-scrollbar {
      width: 6px;
      height: 6px;
    }
    ::-webkit-scrollbar-track {
      background: var(--surface-1);
    }
    ::-webkit-scrollbar-thumb {
      background: var(--surface-3);
      border-radius: 3px;
    }
    ::-webkit-scrollbar-thumb:hover {
      background: rgba(170, 180, 190, 0.25);
    }

    html {
      scroll-behavior: smooth;
    }

    body {
      font-family: var(--font-sans);
      background-color: var(--bg);
      color: var(--text);
      line-height: 1.5;
      min-height: 100vh;
      display: flex;
      flex-direction: column;
      -webkit-font-smoothing: antialiased;
      -moz-osx-font-smoothing: grayscale;
    }

    body::before {
      content: '';
      position: fixed;
      inset: 0;
      pointer-events: none;
      z-index: 60;
      opacity: 0.16;
      background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='160' height='160'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.9' numOctaves='2'/%3E%3C/filter%3E%3Crect width='160' height='160' filter='url(%23n)' opacity='0.55'/%3E%3C/svg%3E");
    }

    body.light::before {
      opacity: 0.07;
    }

    /* Header */
    header {
      background: var(--surface-1);
      border-bottom: 1px solid var(--border);
      padding: 0.875rem 1.75rem;
      display: flex;
      align-items: center;
      justify-content: space-between;
      position: sticky;
      top: 0;
      z-index: 40;
    }

    .brand {
      display: flex;
      align-items: center;
      gap: 0.75rem;
    }

    .brand-logo {
      width: 32px;
      height: 32px;
      background: var(--accent);
      border-radius: var(--radius-sm);
      display: flex;
      align-items: center;
      justify-content: center;
      color: #FFFFFF;
      box-shadow: 0 2px 10px rgba(63, 167, 150, 0.22);
    }

    .brand-logo svg {
      width: 18px;
      height: 18px;
    }

    .brand-title {
      font-size: 1.125rem;
      font-weight: 700;
      letter-spacing: -0.02em;
      color: var(--text);
    }

    .brand-subtitle {
      font-size: 0.75rem;
      color: var(--text-muted);
      font-weight: 500;
    }

    .header-actions {
      display: flex;
      align-items: center;
      gap: 0.75rem;
    }

    .badge {
      display: inline-flex;
      align-items: center;
      gap: 0.375rem;
      padding: 0.25rem 0.625rem;
      font-size: 0.75rem;
      font-weight: 500;
      border-radius: 9999px;
      background: var(--surface-2);
      border: 1px solid var(--border);
      color: var(--text-muted);
    }

    .badge-dot {
      width: 6px;
      height: 6px;
      border-radius: 50%;
      background: var(--accent);
    }

    /* Main Container */
    main {
      flex: 1;
      max-width: 1440px;
      width: 100%;
      margin: 0 auto;
      padding: 1.5rem;
      display: grid;
      grid-template-columns: 460px 1fr;
      gap: 1.5rem;
    }

    @media (max-width: 1080px) {
      main {
        grid-template-columns: 1fr;
      }
    }

    /* Panels */
    .panel {
      background: var(--surface-1);
      border: 1px solid var(--border);
      border-radius: var(--radius-lg);
      display: flex;
      flex-direction: column;
      overflow: hidden;
      box-shadow: var(--shadow-sm);
    }

    .panel-header {
      padding: 1rem 1.25rem;
      border-bottom: 1px solid var(--border);
      display: flex;
      align-items: center;
      justify-content: space-between;
      background: var(--panel-tint);
    }

    .panel-title {
      font-size: 0.9375rem;
      font-weight: 600;
      letter-spacing: -0.01em;
      color: var(--text);
      display: flex;
      align-items: center;
      gap: 0.5rem;
    }

    .panel-title svg {
      width: 16px;
      height: 16px;
      color: var(--text-muted);
    }

    .panel-body {
      padding: 1.25rem;
      flex: 1;
      display: flex;
      flex-direction: column;
      gap: 1.125rem;
    }

    /* Form controls */
    label {
      display: block;
      font-size: 0.8125rem;
      font-weight: 500;
      color: var(--text-muted);
      margin-bottom: 0.375rem;
    }

    .textarea-wrapper {
      position: relative;
      flex: 1;
      min-height: 380px;
      display: flex;
      flex-direction: column;
    }

    textarea {
      width: 100%;
      flex: 1;
      min-height: 380px;
      padding: 0.875rem 1rem;
      background: var(--surface-2);
      border: 1px solid var(--border);
      border-radius: var(--radius-md);
      color: var(--text);
      font-family: var(--font-mono);
      font-size: 0.8125rem;
      line-height: 1.6;
      resize: vertical;
      outline: none;
      transition: border-color 150ms ease, box-shadow 150ms ease;
    }

    textarea:focus {
      border-color: var(--border-focus);
      box-shadow: 0 0 0 2px var(--accent-bg);
    }

    .line-counter {
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 0.375rem 0.25rem;
      font-size: 0.75rem;
      color: var(--text-subtle);
    }

    .input-grid {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 0.75rem;
    }

    select, input[type="text"], input[type="number"] {
      width: 100%;
      padding: 0.5rem 0.75rem;
      background: var(--surface-2);
      border: 1px solid var(--border);
      border-radius: var(--radius-sm);
      color: var(--text);
      font-family: var(--font-sans);
      font-size: 0.8125rem;
      outline: none;
      transition: border-color 150ms ease;
    }

    select:focus, input:focus {
      border-color: var(--border-focus);
    }

    /* Buttons */
    .btn {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 0.5rem;
      padding: 0.5625rem 1rem;
      font-size: 0.8125rem;
      font-weight: 600;
      font-family: var(--font-sans);
      border-radius: var(--radius-sm);
      border: 1px solid transparent;
      cursor: pointer;
      transition: background-color 150ms ease, border-color 150ms ease, opacity 150ms ease;
      text-decoration: none;
      white-space: nowrap;
    }

    .btn:disabled,
    .btn.disabled,
    .btn[aria-disabled="true"] {
      opacity: 0.4;
      pointer-events: none;
      cursor: not-allowed;
    }

    .btn svg {
      width: 15px;
      height: 15px;
    }

    .btn-primary {
      background: var(--accent);
      color: #FFFFFF;
      box-shadow: 0 1px 3px rgba(6, 8, 12, 0.35);
    }
    .btn-primary:hover:not(:disabled) {
      background: var(--accent-hover);
    }
    .btn-primary:active:not(:disabled) {
      background: var(--accent-active);
    }

    .btn-secondary {
      background: var(--surface-2);
      color: var(--text);
      border-color: var(--border);
    }
    .btn-secondary:hover:not(:disabled) {
      background: var(--surface-3);
      border-color: var(--border-hover);
    }

    .btn-success {
      background: var(--accent-active);
      color: #FFFFFF;
    }
    .btn-success:hover:not(:disabled) {
      background: var(--accent-hover);
    }

    .btn:active:not(:disabled) {
      transform: translateY(1px);
    }

    :where(button, a, select, input, textarea):focus-visible {
      outline: 2px solid var(--accent);
      outline-offset: 2px;
    }

    .btn-icon {
      padding: 0.5rem;
      border-radius: var(--radius-sm);
    }

    /* Toggle switch control */
    .toggle-card {
      margin-top: 1rem;
      padding: 0.75rem 0.875rem;
      background: var(--card-tint);
      border: 1px solid var(--border);
      border-radius: var(--radius-md);
      transition: border-color 150ms ease;
    }
    .toggle-card:hover {
      border-color: var(--border-hover);
    }
    .toggle-control {
      display: flex;
      align-items: center;
      gap: 0.75rem;
      cursor: pointer;
      user-select: none;
      margin-bottom: 0;
    }
    .toggle-control input {
      position: absolute;
      opacity: 0;
      width: 0;
      height: 0;
    }
    .toggle-switch {
      width: 38px;
      height: 22px;
      background: var(--surface-3);
      border-radius: 9999px;
      position: relative;
      flex-shrink: 0;
      transition: background-color 200ms ease;
      border: 1px solid var(--border);
    }
    .toggle-switch::after {
      content: '';
      position: absolute;
      top: 2px;
      left: 2px;
      width: 16px;
      height: 16px;
      background: #FFFFFF;
      border-radius: 50%;
      transition: transform 200ms cubic-bezier(0.16, 1, 0.3, 1);
      box-shadow: 0 1px 2px rgba(0, 0, 0, 0.3);
    }
    .toggle-control input:checked + .toggle-switch {
      background: var(--accent);
      border-color: var(--accent-hover);
    }
    .toggle-control input:checked + .toggle-switch::after {
      transform: translateX(16px);
    }
    .toggle-control input:focus-visible + .toggle-switch {
      outline: 2px solid var(--border-focus);
      outline-offset: 2px;
    }
    .toggle-info {
      display: flex;
      flex-direction: column;
      gap: 0.125rem;
    }
    .toggle-title {
      font-size: 0.8125rem;
      font-weight: 600;
      color: var(--text);
    }
    .toggle-desc {
      font-size: 0.6875rem;
      color: var(--text-muted);
    }

    /* Action bar */
    .panel-footer {
      padding: 1rem 1.25rem;
      border-top: 1px solid var(--border);
      background: var(--panel-tint);
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 0.75rem;
    }

    /* Right column: Export Banner & Results Table */
    .export-card {
      background: var(--surface-1);
      border: 1px solid var(--border);
      border-radius: var(--radius-lg);
      padding: 1.25rem;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 1.5rem;
      box-shadow: var(--shadow-sm);
    }

    .export-info h3 {
      font-size: 1rem;
      font-weight: 600;
      color: var(--text);
      letter-spacing: -0.01em;
      margin-bottom: 0.25rem;
    }

    .export-info p {
      font-size: 0.8125rem;
      color: var(--text-muted);
    }

    .export-buttons {
      display: flex;
      align-items: center;
      gap: 0.625rem;
      flex-shrink: 0;
    }

    .text-link {
      color: var(--text-muted);
      text-decoration: none;
      display: inline-flex;
      align-items: center;
      gap: 3px;
      transition: color 120ms ease;
    }

    .text-link:hover {
      color: var(--text);
    }

    /* Stats strip */
    .stats-strip {
      display: grid;
      grid-template-columns: repeat(4, 1fr);
      gap: 0.75rem;
    }

    @media (max-width: 640px) {
      .stats-strip {
        grid-template-columns: repeat(2, 1fr);
      }
    }

    .stat-box {
      background: var(--surface-1);
      border: 1px solid var(--border);
      border-radius: var(--radius-md);
      padding: 0.875rem 1rem;
    }

    .stat-label {
      font-size: 0.75rem;
      color: var(--text-subtle);
      font-weight: 500;
    }

    .stat-value {
      font-size: 1.375rem;
      font-weight: 700;
      color: var(--text);
      margin-top: 0.25rem;
      font-variant-numeric: tabular-nums;
    }

    /* Table Container */
    .table-panel {
      flex: 1;
      display: flex;
      flex-direction: column;
      background: var(--surface-1);
      border: 1px solid var(--border);
      border-radius: var(--radius-lg);
      overflow: hidden;
    }

    .table-toolbar {
      padding: 0.875rem 1.25rem;
      border-bottom: 1px solid var(--border);
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 1rem;
      background: var(--table-tint);
    }

    .search-input-wrapper {
      position: relative;
      flex: 1;
      max-width: 340px;
    }

    .search-input-wrapper input {
      padding-left: 2.25rem;
    }

    .search-icon {
      position: absolute;
      left: 0.75rem;
      top: 50%;
      transform: translateY(-50%);
      width: 14px;
      height: 14px;
      color: var(--text-subtle);
    }

    .table-wrapper {
      flex: 1;
      overflow-y: auto;
      max-height: 520px;
    }

    table {
      width: 100%;
      border-collapse: collapse;
      text-align: left;
      font-size: 0.8125rem;
    }

    thead {
      position: sticky;
      top: 0;
      background: var(--surface-2);
      z-index: 10;
    }

    th {
      padding: 0.75rem 1rem;
      font-size: 0.6875rem;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.05em;
      color: var(--text-muted);
      border-bottom: 1px solid var(--border);
    }

    td {
      padding: 0.6875rem 1rem;
      border-bottom: 1px solid var(--border);
      color: var(--text);
    }

    tbody tr:hover {
      background: var(--row-hover);
    }

    .table-checkbox {
      width: 15px;
      height: 15px;
      accent-color: var(--accent);
      cursor: pointer;
      vertical-align: middle;
    }

    tr.row-excluded {
      opacity: 0.45;
      background: var(--row-excluded-bg);
    }

    tr.row-excluded:hover {
      background: var(--row-hover);
    }

    tr.row-excluded .cell-title {
      text-decoration: line-through;
      color: var(--text-muted);
    }

    tr.row-excluded .tag-type,
    tr.row-excluded .tag-eps,
    tr.row-excluded .status-pill {
      opacity: 0.6;
    }

    .cell-id {
      font-family: var(--font-mono);
      font-size: 0.75rem;
      color: var(--text-muted);
      line-height: 1.35;
    }

    .cell-id a {
      text-decoration: none;
      transition: color 120ms ease;
      color: var(--text-muted);
    }
    .cell-id a:hover {
      color: var(--text);
      text-decoration: underline;
    }

    .cell-title {
      font-weight: 500;
    }

    .cell-original {
      font-size: 0.75rem;
      color: var(--text-subtle);
      margin-top: 0.125rem;
    }

    .tag-type {
      display: inline-block;
      padding: 0.125rem 0.375rem;
      font-size: 0.6875rem;
      font-weight: 600;
      border-radius: var(--radius-sm);
      background: var(--accent-bg);
      color: var(--accent-text);
    }

    .tag-eps {
      font-family: var(--font-mono);
      font-size: 0.75rem;
      color: var(--text-muted);
    }

    .status-pill {
      display: inline-flex;
      align-items: center;
      gap: 0.375rem;
      padding: 0.1875rem 0.5rem;
      font-size: 0.6875rem;
      font-weight: 500;
      border-radius: 9999px;
    }
    .status-completed {
      background: var(--success-bg);
      color: var(--success);
    }
    .status-watching {
      background: var(--accent-bg);
      color: var(--accent-text);
    }
    .status-planned {
      background: var(--planned-bg);
      color: var(--planned-text);
    }
    .status-paused {
      background: var(--paused-bg);
      color: var(--paused-text);
    }
    .status-dropped {
      background: var(--danger-bg);
      color: var(--danger);
    }
    .status-unresolved {
      background: var(--danger-bg);
      color: var(--danger);
    }

    .unresolved-bar {
      display: none;
      align-items: center;
      gap: 0.75rem;
      margin: 0 1rem 0.75rem;
      padding: 0.625rem 0.875rem;
      background: var(--danger-bg);
      border: 1px solid var(--danger);
      border-radius: var(--radius-lg);
      font-size: 0.75rem;
      color: var(--text-muted);
    }
    .unresolved-bar.show { display: flex; }

    .progress-cell {
      display: flex;
      flex-direction: column;
      gap: 0.25rem;
    }
    .progress-cell label {
      display: flex;
      align-items: center;
      gap: 0.3125rem;
      margin: 0;
      font-size: 0.6875rem;
      color: var(--text-subtle);
    }
    .progress-input {
      width: 58px;
      padding: 0.125rem 0.3125rem;
      font-size: 0.6875rem;
      font-family: inherit;
      color: var(--text);
      background: var(--surface-2);
      border: 1px solid var(--border);
      border-radius: 4px;
    }
    .progress-input:focus {
      outline: none;
      border-color: var(--border-focus);
    }
    .progress-input:disabled {
      opacity: 0.4;
      cursor: not-allowed;
    }
    .match-picker {
      width: 100%;
      margin-top: 0.3125rem;
      padding: 0.1875rem 0.3125rem;
      font-size: 0.6875rem;
      font-family: inherit;
      color: var(--text-muted);
      background: var(--surface-2);
      border: 1px solid var(--border);
      border-radius: 4px;
    }
    .match-picker:focus {
      outline: none;
      border-color: var(--border-focus);
    }
    .unresolved-bar strong { color: var(--danger); }
    .unresolved-bar .spacer { flex: 1; }

    /* Live Progress Card */
    .progress-card {
      background: var(--surface-1);
      border: 1px solid var(--accent-tint);
      border-radius: var(--radius-lg);
      padding: 1rem 1.25rem;
      box-shadow: var(--shadow-md), 0 0 0 1px var(--accent-bg);
      display: flex;
      flex-direction: column;
      gap: 0.75rem;
      animation: fadeInProgress 200ms ease;
    }

    @keyframes fadeInProgress {
      from { opacity: 0; transform: translateY(-4px); }
      to { opacity: 1; transform: translateY(0); }
    }

    .progress-card-header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 1rem;
    }

    .progress-card-status {
      display: flex;
      align-items: center;
      gap: 0.75rem;
      min-width: 0;
      flex: 1;
    }

    .progress-spinner {
      width: 18px;
      height: 18px;
      border: 2px solid var(--accent-bg);
      border-top-color: var(--accent);
      border-radius: 50%;
      flex-shrink: 0;
      animation: spin-progress 0.7s linear infinite;
    }

    @keyframes spin-progress {
      to { transform: rotate(360deg); }
    }

    .progress-current-title {
      font-size: 0.875rem;
      font-weight: 600;
      color: var(--text);
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }

    .progress-current-sub {
      font-size: 0.75rem;
      color: var(--text-muted);
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }

    .progress-badge-group {
      display: flex;
      align-items: center;
      gap: 0.5rem;
      flex-shrink: 0;
    }

    .progress-count-pill {
      font-family: var(--font-mono);
      font-size: 0.75rem;
      font-weight: 600;
      padding: 0.25rem 0.625rem;
      background: var(--surface-2);
      border: 1px solid var(--border);
      border-radius: 9999px;
      color: var(--text);
    }

    .progress-percent-pill {
      font-family: var(--font-mono);
      font-size: 0.75rem;
      font-weight: 700;
      padding: 0.25rem 0.625rem;
      background: var(--accent-bg);
      border-radius: 9999px;
      color: var(--accent-text);
    }

    .progress-track {
      width: 100%;
      height: 8px;
      background: var(--surface-2);
      border-radius: 9999px;
      overflow: hidden;
      position: relative;
    }

    .progress-fill {
      width: 100%;
      height: 100%;
      background: var(--accent);
      border-radius: 9999px;
      transform-origin: left center;
      transform: scaleX(0);
      transition: transform 180ms cubic-bezier(0.16, 1, 0.3, 1);
    }

    /* Empty state */
    .empty-state {
      padding: 3.5rem 1.5rem;
      text-align: center;
      color: var(--text-muted);
    }
    .empty-state svg {
      width: 40px;
      height: 40px;
      color: var(--text-subtle);
      margin-bottom: 0.75rem;
    }
    .empty-state h4 {
      font-size: 0.9375rem;
      font-weight: 600;
      color: var(--text);
      margin-bottom: 0.25rem;
    }
    .empty-state p {
      font-size: 0.8125rem;
      max-width: 360px;
      margin: 0 auto;
    }

    /* Toast notification */
    .toast {
      position: fixed;
      bottom: 1.5rem;
      right: 1.5rem;
      padding: 0.75rem 1rem;
      background: var(--surface-2);
      border: 1px solid var(--border);
      border-radius: var(--radius-md);
      box-shadow: var(--shadow-lg);
      font-size: 0.8125rem;
      color: var(--text);
      display: flex;
      align-items: center;
      gap: 0.5rem;
      transform: translateY(100px);
      opacity: 0;
      transition: transform 200ms cubic-bezier(0.16, 1, 0.3, 1), opacity 200ms ease;
      z-index: 50;
    }
    .toast.show {
      transform: translateY(0);
      opacity: 1;
    }
    .toast.toast-success {
      border-color: var(--success);
    }
    .toast.toast-error {
      border-color: var(--danger);
    }
  </style>
</head>
<body>

  <!-- Top App Navigation -->
  <header>
    <div class="brand">
      <div class="brand-logo" aria-hidden="true">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">
          <polygon points="12 2 2 7 12 12 22 7 12 2"></polygon>
          <polyline points="2 17 12 22 22 17"></polyline>
          <polyline points="2 12 12 17 22 12"></polyline>
        </svg>
      </div>
      <div>
        <h1 class="brand-title">AniExport</h1>
        <p class="brand-subtitle">Universal Title Normalizer &bull; XML Export for MyAnimeList &amp; AniList</p>
      </div>
    </div>

    <div class="header-actions">
      <button id="btnTheme" class="btn btn-secondary" style="padding:0.375rem 0.75rem; font-size:0.75rem;" title="Switch color theme">Light mode</button>
      <div class="badge" title="Universal XML format supported natively by MyAnimeList and AniList import">
        <span class="badge-dot" aria-hidden="true"></span>
        <span>MAL &amp; AniList Compatible</span>
      </div>
    </div>
  </header>

  <!-- Main View -->
  <main>
    <!-- Left Column: Plain text editor and parameters -->
    <section class="panel">
      <div class="panel-header">
        <h2 class="panel-title">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <path d="M16 4h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h2"></path>
            <rect x="8" y="2" width="8" height="4" rx="1" ry="1"></rect>
          </svg>
          <span id="listPanelTitle">Paste Anime List</span>
        </h2>
        <div style="display:flex; gap:0.375rem;">
          <button id="btnSample" class="btn btn-secondary" style="padding:0.25rem 0.5rem; font-size:0.75rem;" title="Load sample list">
            Sample
          </button>
          <button id="btnClear" class="btn btn-secondary" style="padding:0.25rem 0.5rem; font-size:0.75rem;" title="Clear text area">
            Clear
          </button>
        </div>
      </div>

      <div class="panel-body">
        <div>
          <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:0.375rem;">
            <label for="animeTextInput" style="margin-bottom:0;">Paste your list (one title per line):</label>
            <button id="btnPaste" class="btn btn-secondary" style="padding:0.1875rem 0.5rem; font-size:0.6875rem;" title="Paste clipboard content">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="width:12px; height:12px;">
                <path d="M16 4h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h2"></path>
                <rect x="8" y="2" width="8" height="4" rx="1" ry="1"></rect>
              </svg>
              Paste
            </button>
          </div>
          <div class="textarea-wrapper">
            <textarea id="animeTextInput" spellcheck="false" placeholder="Paste anime titles here (one per line):&#10;&#10;Sousou no Frieren&#10;Attack on Titan&#10;Solo Leveling&#10;Dandadan&#10;Chainsaw Man&#10;..."></textarea>
            <div class="line-counter">
              <span id="lineCountLabel">0 titles</span>
              <span style="font-size:0.6875rem;">Strips numbering and bullets</span>
            </div>
          </div>
        </div>

        <div>
          <label for="mediaType">Media Type:</label>
          <select id="mediaType">
            <option value="anime" selected>Anime</option>
            <option value="manga">Manga</option>
          </select>
        </div>

        <div class="input-grid">
          <div>
            <label for="selectStatus">List Status:</label>
            <select id="selectStatus">
              <option value="Watching">Watching</option>
              <option value="Plan to watch">Plan to watch</option>
              <option value="Completed" selected>Completed</option>
              <option value="Rewatching">Rewatching</option>
              <option value="Paused">Paused</option>
              <option value="Dropped">Dropped</option>
            </select>
          </div>
          <div>
            <label for="inputScore">Score (0-10):</label>
            <input type="number" id="inputScore" min="0" max="10" value="0" placeholder="0 = Unscored">
          </div>
        </div>

        <div class="toggle-card">
          <label class="toggle-control" for="toggleSequels">
            <input type="checkbox" id="toggleSequels">
            <span class="toggle-switch" aria-hidden="true"></span>
            <span class="toggle-info">
              <span class="toggle-title">Auto-expand sequels &amp; movies</span>
              <span class="toggle-desc">Automatically detects subsequent seasons and sequel movies</span>
            </span>
          </label>
        </div>
      </div>

      <div class="panel-footer" style="justify-content:flex-end;">
        <button id="btnProcess" class="btn btn-primary" style="width:100%;">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <polygon points="5 3 19 12 5 21 5 3"></polygon>
          </svg>
          Normalize &amp; Generate XML
        </button>
      </div>
    </section>

    <!-- Right Column: Quick Status Banner and Results Preview -->
    <div style="display:flex; flex-direction:column; gap:1.25rem;">
      
      <!-- Top Card: Download Actions & Ready Status -->
      <section class="export-card">
        <div class="export-info">
          <h3 id="exportHeadline">Export Pending</h3>
          <p id="exportSubline">Paste your anime list on the left and click "Normalize &amp; Generate XML".</p>
          <div style="margin-top:0.375rem; display:flex; gap:0.875rem; font-size:0.75rem;">
            <a href="https://myanimelist.net/import.php" target="_blank" rel="noopener noreferrer" class="text-link" title="Open MyAnimeList Import page">
              <span>MyAnimeList Import</span>
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="width:11px; height:11px;"><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"></path><polyline points="15 3 21 3 21 9"></polyline><line x1="10" y1="14" x2="21" y2="3"></line></svg>
            </a>
            <a href="https://anilist.co/settings/import" target="_blank" rel="noopener noreferrer" class="text-link" title="Open AniList Import page">
              <span>AniList Import</span>
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="width:11px; height:11px;"><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"></path><polyline points="15 3 21 3 21 9"></polyline><line x1="10" y1="14" x2="21" y2="3"></line></svg>
            </a>
          </div>
        </div>
        <div class="export-buttons">
          <a id="btnDownloadXml" href="/api/download/xml" class="btn btn-secondary disabled" aria-disabled="true" download="myanimelist_export.xml">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path>
              <polyline points="7 10 12 15 17 10"></polyline>
              <line x1="12" y1="15" x2="12" y2="3"></line>
            </svg>
            Download XML
          </a>
          <a id="btnDownloadGz" href="/api/download/gz" class="btn btn-primary disabled" aria-disabled="true" download="myanimelist_export.xml.gz">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path>
              <polyline points="7 10 12 15 17 10"></polyline>
              <line x1="12" y1="15" x2="12" y2="3"></line>
            </svg>
            Download .XML.GZ
          </a>
        </div>
      </section>

      <!-- Stats Strip -->
      <div class="stats-strip">
        <div class="stat-box">
          <div class="stat-label">Total Entries</div>
          <div id="statTotal" class="stat-value">0</div>
        </div>
        <div class="stat-box">
          <div class="stat-label">Included in Export</div>
          <div id="statNormalized" class="stat-value">0</div>
        </div>
        <div class="stat-box">
          <div class="stat-label">Total Episodes / Chapters</div>
          <div id="statEpisodes" class="stat-value">0</div>
        </div>
        <div class="stat-box">
          <div class="stat-label">XML Status</div>
          <div id="statXmlValid" class="stat-value" style="font-size:1.125rem; color:var(--text-subtle);">Not Generated</div>
        </div>
      </div>

      <!-- Live Progress Card -->
      <section id="progressCard" class="progress-card" style="display: none;">
        <div class="progress-card-header">
          <div class="progress-card-status">
            <span class="progress-spinner" aria-hidden="true"></span>
            <div style="min-width: 0;">
              <div id="progressCurrentTitle" class="progress-current-title">Starting normalization...</div>
              <div id="progressCurrentSub" class="progress-current-sub">Connecting to AniList GraphQL...</div>
            </div>
          </div>
          <div class="progress-badge-group">
            <span id="progressCountLabel" class="progress-count-pill">0 / 0</span>
            <span id="progressPercentLabel" class="progress-percent-pill">0%</span>
            <span id="etaLabel" class="progress-count-pill"></span>
          </div>
        </div>
        <div class="progress-track" role="progressbar" aria-valuenow="0" aria-valuemin="0" aria-valuemax="100">
          <div id="progressBarFill" class="progress-fill"></div>
        </div>
      </section>

      <!-- Table Panel -->
      <section class="table-panel">
        <div id="unresolvedBar" class="unresolved-bar">
          <span id="unresolvedText"></span>
          <span class="spacer"></span>
          <button id="btnCopyUnresolved" class="btn btn-secondary" style="padding:0.1875rem 0.5rem; font-size:0.6875rem;">Copy unmatched titles</button>
        </div>
        <div class="table-toolbar">
          <div class="search-input-wrapper">
            <svg class="search-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <circle cx="11" cy="11" r="8"></circle>
              <line x1="21" y1="21" x2="16.65" y2="16.65"></line>
            </svg>
            <input type="text" id="searchInput" placeholder="Filter by title, MAL ID, or AniList ID...">
          </div>
          <span id="filterCountLabel" style="font-size:0.75rem; color:var(--text-muted);">Showing 0</span>
        </div>

        <div class="table-wrapper">
          <table id="animeTable">
            <thead>
              <tr>
                <th style="width: 42px; text-align: center;">
                  <input type="checkbox" id="selectAllCheckbox" class="table-checkbox" checked title="Select/Deselect all">
                </th>
                <th style="width: 105px;">Database IDs</th>
                <th>Official Title (Full English)</th>
                <th style="width: 90px;">Type</th>
                <th style="width: 110px;" id="countColumnHeading">Episodes</th>
                <th style="width: 125px;" id="progressColumnHeading">Progress</th>
                <th style="width: 100px;">Status</th>
              </tr>
            </thead>
            <tbody id="tableBody">
              <!-- Dynamically rendered rows -->
            </tbody>
          </table>

          <div id="emptyTableState" class="empty-state">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
              <circle cx="12" cy="12" r="10"></circle>
              <line x1="12" y1="8" x2="12" y2="12"></line>
              <line x1="12" y1="16" x2="12.01" y2="16"></line>
            </svg>
            <h4>No titles processed yet</h4>
            <p>Paste your list of titles on the left and click <strong>Normalize &amp; Generate XML</strong>.</p>
          </div>
        </div>
      </section>

    </div>
  </main>

  <!-- Toast -->
  <div id="toast" class="toast">
    <span id="toastMessage">Action completed</span>
  </div>

  <script>
    let currentAnimes = [];
    let isProcessing = false;

    // Elements
    const txtArea = document.getElementById('animeTextInput');
    const mediaTypeSelect = document.getElementById('mediaType');
    const lineCountLabel = document.getElementById('lineCountLabel');
    const selectStatus = document.getElementById('selectStatus');
    const inputScore = document.getElementById('inputScore');
    const btnProcess = document.getElementById('btnProcess');
    const btnPaste = document.getElementById('btnPaste');
    const btnClear = document.getElementById('btnClear');
    const btnSample = document.getElementById('btnSample');
    const toggleSequels = document.getElementById('toggleSequels');
    const searchInput = document.getElementById('searchInput');
    const tableBody = document.getElementById('tableBody');
    const emptyState = document.getElementById('emptyTableState');
    const selectAllCheckbox = document.getElementById('selectAllCheckbox');
    const progressCard = document.getElementById('progressCard');
    const progressBarFill = document.getElementById('progressBarFill');
    const progressCurrentTitle = document.getElementById('progressCurrentTitle');
    const progressCurrentSub = document.getElementById('progressCurrentSub');
    const progressCountLabel = document.getElementById('progressCountLabel');
    const progressPercentLabel = document.getElementById('progressPercentLabel');
    const etaLabel = document.getElementById('etaLabel');
    const exportHeadline = document.getElementById('exportHeadline');
    const exportSubline = document.getElementById('exportSubline');
    const btnDownloadXml = document.getElementById('btnDownloadXml');
    const btnDownloadGz = document.getElementById('btnDownloadGz');
    const statTotal = document.getElementById('statTotal');
    const statNormalized = document.getElementById('statNormalized');
    const statEpisodes = document.getElementById('statEpisodes');
    const statXmlValid = document.getElementById('statXmlValid');
    const filterCountLabel = document.getElementById('filterCountLabel');
    const toast = document.getElementById('toast');
    const toastMessage = document.getElementById('toastMessage');
    const countColumnHeading = document.getElementById('countColumnHeading');
    const unresolvedBar = document.getElementById('unresolvedBar');
    const unresolvedText = document.getElementById('unresolvedText');
    const btnCopyUnresolved = document.getElementById('btnCopyUnresolved');

    // AniList's own vocabulary. The server maps these onto MAL's XML values:
    // Paused -> On-Hold, Rereading/Rewatching -> Reading/Watching + the repeat flag.
    const STATUS_SETS = {
      manga: ['Reading', 'Plan to read', 'Completed', 'Rereading', 'Paused', 'Dropped'],
      anime: ['Watching', 'Plan to watch', 'Completed', 'Rewatching', 'Paused', 'Dropped'],
    };

    const STATUS_PILL = {
      'reading': 'status-watching', 'watching': 'status-watching',
      'rereading': 'status-watching', 'rewatching': 'status-watching',
      'completed': 'status-completed',
      'plan to read': 'status-planned', 'plan to watch': 'status-planned',
      'paused': 'status-paused', 'on-hold': 'status-paused',
      'dropped': 'status-dropped',
    };

    // Mirrors the server: a finished entry (or an in-progress repeat) counts as
    // fully read/watched; anything else starts at zero. Shown as a placeholder so
    // an untouched field stays derived rather than frozen at today's value.
    function isRepeatStatus(status) {
      const s = String(status || '').toLowerCase();
      return s === 'rereading' || s === 'rewatching';
    }

    function derivedProgress(item, field) {
      const done = String(item.status || '').toLowerCase() === 'completed' || isRepeatStatus(item.status);
      const total = field === 'my_read_volumes' ? item.volumes
                  : field === 'my_read_chapters' ? item.chapters
                  : item.episodes;
      return done ? (Number(total) || 0) : 0;
    }

    const PROGRESS_TOTAL_FIELD = {
      'my_read_chapters': 'chapters',
      'my_read_volumes': 'volumes',
      'my_watched_episodes': 'episodes',
    };

    function progressFields() {
      return mediaType() === 'manga'
        ? [['my_read_chapters', 'ch'], ['my_read_volumes', 'vol']]
        : [['my_watched_episodes', 'ep']];
    }

    function mediaType() {
      return mediaTypeSelect.value === 'manga' ? 'manga' : 'anime';
    }

    function updateMediaTypeUI(clearResults = false) {
      const isManga = mediaType() === 'manga';
      // Same order in both sets, so a switch carries the equivalent state across
      // by index (Reading <-> Watching, Rereading <-> Rewatching, ...).
      const from = STATUS_SETS[isManga ? 'anime' : 'manga'];
      const to = STATUS_SETS[isManga ? 'manga' : 'anime'];
      const previous = selectStatus.value;
      const sameIdx = from.indexOf(previous);
      const carried = sameIdx >= 0 ? to[sameIdx] : (to.indexOf(previous) >= 0 ? previous : 'Completed');
      selectStatus.innerHTML = to.map(v => `<option value="${escapeHtml(v)}">${escapeHtml(v)}</option>`).join('');
      selectStatus.value = carried;
      document.getElementById('listPanelTitle').textContent = isManga ? 'Paste Manga List' : 'Paste Anime List';
      document.querySelector('label[for="animeTextInput"]').textContent = `Paste your list (one ${isManga ? 'manga' : 'anime'} per line):`;
      countColumnHeading.textContent = isManga ? 'Chapters / Volumes' : 'Episodes';
      document.getElementById('progressColumnHeading').textContent = isManga ? 'Read' : 'Watched';
      const statLabels = document.querySelectorAll('.stat-label');
      if (statLabels[2]) statLabels[2].textContent = isManga ? 'Total Chapters' : 'Total Episodes';
      toggleSequels.closest('.toggle-card').style.display = isManga ? 'none' : '';
      // NOTE: this template is a plain Python string, so a backslash-n here would
      // become a real newline and break the whole <script>. Build newlines in JS.
      txtArea.placeholder = (isManga
        ? ['Paste manga titles here (one per line):', '', 'Berserk', 'One Piece', 'Vagabond', 'Fullmetal Alchemist', '...']
        : ['Paste anime titles here (one per line):', '', 'Sousou no Frieren', 'Attack on Titan', 'Solo Leveling', '...']
      ).join(String.fromCharCode(10));
      btnDownloadXml.download = isManga ? 'myanimelist_manga_export.xml' : 'myanimelist_export.xml';
      btnDownloadGz.download = isManga ? 'myanimelist_manga_export.xml.gz' : 'myanimelist_export.xml.gz';
      if (clearResults) {
        currentAnimes = [];
        renderTable([]);
        updateStatsAndExport();
      }
      updateLineCounter();
    }

    mediaTypeSelect.addEventListener('change', () => updateMediaTypeUI(true));

    function showToast(msg, type = 'success') {
      toastMessage.textContent = msg;
      toast.className = 'toast show toast-' + type;
      setTimeout(() => {
        toast.className = 'toast';
      }, 3500);
    }

    function updateLineCounter() {
      const text = txtArea.value;
      const lines = text.split(String.fromCharCode(10)).filter(l => l.trim() && !l.trim().startsWith('#'));
      lineCountLabel.textContent = lines.length + (mediaType() === 'manga' ? ' manga' : ' anime');
    }

    txtArea.addEventListener('input', updateLineCounter);

    // Paste from clipboard
    btnPaste.addEventListener('click', async () => {
      try {
        const text = await navigator.clipboard.readText();
        if (text) {
          txtArea.value = text;
          updateLineCounter();
          showToast('Pasted from clipboard');
        } else {
          txtArea.focus();
          showToast('Clipboard is empty', 'warning');
        }
      } catch (e) {
        txtArea.focus();
        showToast('Press Ctrl+V to paste into the text area', 'warning');
      }
    });

    // Clear textarea
    btnClear.addEventListener('click', () => {
      txtArea.value = '';
      updateLineCounter();
      showToast('Text area cleared');
    });

    // Load sample list
    btnSample.addEventListener('click', () => {
      const sampleTitles = mediaType() === 'manga'
        ? ['Berserk', 'One Piece', 'Vagabond', 'Fullmetal Alchemist', 'Dandadan', 'Chainsaw Man', 'Vinland Saga']
        : ['Sousou no Frieren', 'Shingeki no Kyojin', 'Solo Leveling', 'Dandadan', 'Chainsaw Man', 'KonoSuba', 'Jujutsu Kaisen'];
      txtArea.value = sampleTitles.join(String.fromCharCode(10));
      updateLineCounter();
      showToast('Sample list loaded');
    });

    // Start completely empty every time
    async function loadData() {
      renderTable([]);
      statTotal.textContent = '0';
      statNormalized.textContent = '0';
      statEpisodes.textContent = '0';
      statXmlValid.textContent = 'Not Generated';
      statXmlValid.style.color = 'var(--text-subtle)';
    }

    // Process & Normalize
    btnProcess.addEventListener('click', async () => {
      if (isProcessing) return;
      isProcessing = true;
      btnProcess.disabled = true;

      try {
        const payload = {
          content: txtArea.value,
          media_type: mediaType(),
          status: selectStatus.value,
          score: parseInt(inputScore.value, 10) || 0,
          expand_sequels: mediaType() === 'anime' && toggleSequels.checked
        };

        progressCard.style.display = 'flex';
        progressBarFill.style.transform = 'scaleX(0)';
        progressCountLabel.textContent = '0 / 0';
        progressPercentLabel.textContent = '0%';
        progressCurrentTitle.textContent = 'Starting normalization...';
        progressCurrentSub.textContent = 'Connecting to AniList GraphQL...';

        const res = await fetch('/api/process', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload)
        });

        if (!res.ok) {
          const errData = await res.json().catch(() => ({}));
          showToast(errData.error || 'Error processing list', 'error');
          progressCard.style.display = 'none';
          return;
        }

        const reader = res.body.getReader();
        const decoder = new TextDecoder('utf-8');
        let buffer = '';

        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });
          const parts = buffer.split(String.fromCharCode(10) + String.fromCharCode(10));
          buffer = parts.pop();

          for (const chunk of parts) {
            const trimmed = chunk.trim();
            if (!trimmed.startsWith('data: ')) continue;
            try {
              const event = JSON.parse(trimmed.slice(6));
              if (event.type === 'progress') {
                const ratio = Math.min(1, Math.max(0, event.percent / 100));
                progressBarFill.style.transform = 'scaleX(' + ratio + ')';
                progressCountLabel.textContent = event.current + ' / ' + event.total;
                if (event.interval) {
                  const left = Math.max(0, event.total - event.current) * event.interval;
                  const mins = Math.floor(left / 60), secs = Math.round(left % 60);
                  progressCountLabel.title = `~${mins > 0 ? mins + 'm ' : ''}${secs}s remaining at AniList's rate limit`;
                  etaLabel.textContent = left > 0 ? `~${mins > 0 ? mins + 'm ' : ''}${secs}s left` : 'finishing...';
                }
                progressPercentLabel.textContent = Math.round(event.percent) + '%';
                progressCurrentTitle.textContent = `[${event.current}/${event.total}] ${event.title}`;
                if (event.resolved_title) {
                  progressCurrentSub.textContent = '-> ' + event.resolved_title;
                } else {
                  progressCurrentSub.textContent = 'Resolving metadata via AniList & MAL...';
                }
              } else if (event.type === 'rate_limit') {
                progressCurrentSub.textContent = `AniList rate limit reached (30 requests/min) - waiting ${event.seconds}s, this is normal for long lists...`;
              } else if (event.type === 'complete' && event.ok) {
                progressBarFill.style.transform = 'scaleX(1)';
                progressPercentLabel.textContent = '100%';
                currentAnimes = event.entries.map(e => ({ ...e, enabled: Number(e.id) > 0 }));
                selectAllCheckbox.checked = true;
                selectAllCheckbox.indeterminate = false;
                renderTable(currentAnimes);
                updateStatsAndExport();
                showToast('XML generated (' + event.entries.length + ' ' + (mediaType() === 'manga' ? 'manga' : 'anime') + ' rows; unresolved rows are excluded)');
                try { reader.cancel(); } catch(e) {}
                break;
              }
            } catch (errParse) {
              // Ignore partial JSON
            }
          }
        }
      } catch (err) {
        showToast('Error communicating with local server', 'error');
      } finally {
        isProcessing = false;
        btnProcess.disabled = false;
        setTimeout(() => {
          progressCard.style.display = 'none';
        }, 1500);
      }
    });

    let exportDebounceTimer = null;

    function filteredCount() {
      const q = searchInput.value.toLowerCase().trim();
      if (!q) return currentAnimes.length;
      return currentAnimes.filter(item => {
        const title = (item.title || '').toLowerCase();
        const orig = (item.original || '').toLowerCase();
        const malId = String(item.id || '');
        const alId = String(item.anilist_id || '');
        return title.includes(q) || orig.includes(q) || malId.includes(q) || alId.includes(q);
      }).length;
    }

    function updateStatsAndExport() {
      const enabledItems = currentAnimes.filter(i => i.enabled !== false && Number(i.id) > 0);
      const totalCount = currentAnimes.length;
      const enabledCount = enabledItems.length;

      statTotal.textContent = totalCount;
      statNormalized.textContent = enabledCount === totalCount ? String(totalCount) : `${enabledCount} of ${totalCount}`;
      const totalEps = enabledItems.reduce((acc, curr) => acc + (mediaType() === 'manga' ? (curr.chapters || 0) : (curr.episodes || 0)), 0);
      statEpisodes.textContent = totalEps.toLocaleString();

      filterCountLabel.textContent = `Showing ${filteredCount()} of ${totalCount} (${enabledCount} selected)`;

      const missed = currentAnimes.filter(i => Number(i.id) <= 0);
      unresolvedBar.classList.toggle('show', missed.length > 0);
      if (missed.length > 0) {
        const noun = mediaType() === 'manga' ? 'manga' : 'anime';
        unresolvedText.innerHTML = `<strong>${missed.length}</strong> ${noun} ${missed.length === 1 ? 'title has' : 'titles have'} no MyAnimeList ID on AniList and cannot be exported. Check the spelling, or try the romaji title.`;
      }

      if (enabledCount > 0) {
        exportHeadline.textContent = `Export files ready (${enabledCount} ${mediaType() === 'manga' ? 'manga' : 'anime'})`;
        exportSubline.textContent = 'MAL-compatible XML for import through MyAnimeList or AniList.';
        btnDownloadXml.classList.remove('disabled');
        btnDownloadXml.removeAttribute('aria-disabled');
        btnDownloadGz.classList.remove('disabled');
        btnDownloadGz.removeAttribute('aria-disabled');
        statXmlValid.textContent = 'Valid';
        statXmlValid.style.color = 'var(--success)';
      } else {
        exportHeadline.textContent = `No ${mediaType() === 'manga' ? 'manga' : 'anime'} selected`;
        exportSubline.textContent = 'Select at least one anime checkbox to enable export download.';
        btnDownloadXml.classList.add('disabled');
        btnDownloadXml.setAttribute('aria-disabled', 'true');
        btnDownloadGz.classList.add('disabled');
        btnDownloadGz.setAttribute('aria-disabled', 'true');
        statXmlValid.textContent = 'Empty';
        statXmlValid.style.color = 'var(--text-muted)';
      }

      clearTimeout(exportDebounceTimer);
      exportDebounceTimer = setTimeout(async () => {
        try {
          await fetch('/api/update-export', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              entries: enabledItems.map(({ candidates, ...rest }) => rest),
              media_type: mediaType(),
              status: selectStatus.value,
              score: parseInt(inputScore.value, 10) || 0
            })
          });
        } catch (e) {
          // ignore background sync error
        }
      }, 250);
    }

    function updateSelectAllState() {
      const q = searchInput.value.toLowerCase().trim();
      const visible = currentAnimes.filter(item => {
        if (!q) return true;
        const title = (item.title || '').toLowerCase();
        const orig = (item.original || '').toLowerCase();
        const malId = String(item.id || '');
        const alId = String(item.anilist_id || '');
        return title.includes(q) || orig.includes(q) || malId.includes(q) || alId.includes(q);
      });
      if (visible.length === 0) {
        selectAllCheckbox.checked = false;
        selectAllCheckbox.indeterminate = false;
        return;
      }
      const allChecked = visible.every(i => i.enabled !== false);
      const someChecked = visible.some(i => i.enabled !== false);
      selectAllCheckbox.checked = allChecked;
      selectAllCheckbox.indeterminate = !allChecked && someChecked;
    }

    function pickerHtml(item, idx) {
      const list = Array.isArray(item.candidates) ? item.candidates : [];
      if (list.length < 2) return '';
      const options = list.map((c, i) => {
        const counts = mediaType() === 'manga'
          ? `${c.chapters || '?'} ch`
          : `${c.episodes || '?'} eps`;
        const label = `${c.title} - MAL ${c.id} - ${c.type} - ${counts}`;
        return `<option value="${i}" ${Number(c.id) === Number(item.id) ? 'selected' : ''}>${escapeHtml(label)}</option>`;
      }).join('');
      return `<select class="match-picker" data-index="${idx}" title="Pick the correct match for this title">${options}</select>`;
    }

    function progressHtml(item, idx) {
      const exportable = Number(item.id) > 0;
      return '<div class="progress-cell">' + progressFields().map(([field, unit]) => {
        const value = item[field] === undefined || item[field] === null ? '' : item[field];
        const total = Number(item[PROGRESS_TOTAL_FIELD[field]] || 0);
        return `<label><input type="number" class="progress-input" min="0" ${total > 0 ? `max="${total}"` : ''}
          data-index="${idx}" data-field="${field}" value="${value}"
          placeholder="${derivedProgress(item, field)}" ${exportable ? '' : 'disabled'}
          aria-label="${escapeHtml(unit)} read for ${escapeHtml(item.title)}">${unit}</label>`;
      }).join('') + '</div>';
    }

    // Render Table
    function renderTable(items) {
      const q = searchInput.value.toLowerCase().trim();
      const filtered = items.filter(item => {
        if (!q) return true;
        const title = (item.title || '').toLowerCase();
        const orig = (item.original || '').toLowerCase();
        const malId = String(item.id || '');
        const alId = String(item.anilist_id || '');
        return title.includes(q) || orig.includes(q) || malId.includes(q) || alId.includes(q);
      });

      const enabledCount = currentAnimes.filter(i => i.enabled !== false).length;
      filterCountLabel.textContent = 'Showing ' + filtered.length + ' of ' + items.length + ' (' + enabledCount + ' selected)';

      if (filtered.length === 0) {
        tableBody.innerHTML = '';
        emptyState.style.display = 'block';
        updateSelectAllState();
        return;
      }

      emptyState.style.display = 'none';
      tableBody.innerHTML = filtered.map(item => {
        const globalIdx = currentAnimes.indexOf(item);
        const malUrl = item.id ? `https://myanimelist.net/${mediaType()}/${item.id}` : '';
        const alUrl = item.anilist_id ? `https://anilist.co/${mediaType()}/${item.anilist_id}` : '';
        const hasRenamed = item.original && item.original.toLowerCase() !== item.title.toLowerCase();
        const isChecked = item.enabled !== false && Number(item.id) > 0;
        const pillClass = STATUS_PILL[String(item.status || '').toLowerCase()] || 'status-completed';
        return `
          <tr class="${isChecked ? '' : 'row-excluded'}">
            <td class="cell-checkbox" style="text-align: center; cursor: pointer;">
              <input type="checkbox" class="table-checkbox row-checkbox" data-index="${globalIdx}" ${isChecked ? 'checked' : ''} ${Number(item.id) > 0 ? '' : 'disabled'} aria-label="Include ${escapeHtml(item.title)}">
            </td>
            <td class="cell-id">
              <div style="display:flex; flex-direction:column; gap:2px;">
                ${malUrl ? `<a href="${malUrl}" class="id-link" target="_blank" rel="noopener noreferrer" title="View on MyAnimeList"><span style="color:var(--text-subtle); font-size:0.625rem; font-weight:600; margin-right:4px;">MAL</span>${item.id}</a>` : '<span style="color:var(--text-subtle);"><span style="font-size:0.625rem; font-weight:600; margin-right:4px;">MAL</span>-</span>'}
                ${alUrl ? `<a href="${alUrl}" class="id-link" target="_blank" rel="noopener noreferrer" title="View on AniList"><span style="color:var(--text-subtle); font-size:0.625rem; font-weight:600; margin-right:4px;">AL</span>${item.anilist_id}</a>` : '<span style="color:var(--text-subtle);"><span style="font-size:0.625rem; font-weight:600; margin-right:4px;">AL</span>-</span>'}
              </div>
            </td>
            <td>
              <div class="cell-title">${escapeHtml(item.title)}</div>
              ${hasRenamed ? `<div class="cell-original">Original: ${escapeHtml(item.original)}</div>` : ''}
              ${pickerHtml(item, globalIdx)}
            </td>
            <td><span class="${Number(item.id) > 0 ? 'tag-type' : 'status-pill status-unresolved'}">${escapeHtml(Number(item.id) > 0 ? (item.type || 'TV') : 'No MAL ID')}</span></td>
            <td><span class="tag-eps">${mediaType() === 'manga' ? `${item.chapters || 0} ch / ${item.volumes || 0} vol` : `${item.episodes || 0} eps`}</span></td>
            <td>${progressHtml(item, globalIdx)}</td>
            <td><span class="status-pill ${pillClass}">${escapeHtml(item.status || 'Completed')}</span></td>
          </tr>
        `;
      }).join('');

      updateSelectAllState();
    }

    selectAllCheckbox.addEventListener('change', () => {
      const isChecked = selectAllCheckbox.checked;
      const q = searchInput.value.toLowerCase().trim();
      currentAnimes.forEach(item => {
        if (Number(item.id) <= 0) { item.enabled = false; return; }
        if (!q) {
          item.enabled = isChecked;
        } else {
          const title = (item.title || '').toLowerCase();
          const orig = (item.original || '').toLowerCase();
          const malId = String(item.id || '');
          const alId = String(item.anilist_id || '');
          if (title.includes(q) || orig.includes(q) || malId.includes(q) || alId.includes(q)) {
            item.enabled = isChecked;
          }
        }
      });
      renderTable(currentAnimes);
      updateStatsAndExport();
    });

    tableBody.addEventListener('change', (e) => {
      if (e.target && e.target.classList.contains('match-picker')) {
        const idx = parseInt(e.target.dataset.index, 10);
        const item = currentAnimes[idx];
        const pick = item && Array.isArray(item.candidates)
          ? item.candidates[parseInt(e.target.value, 10)] : null;
        if (!pick) return;
        // Replace the identity/metadata only; status, score and progress stay.
        Object.assign(item, pick, { enabled: Number(pick.id) > 0 });
        renderTable(currentAnimes);
        updateStatsAndExport();
        showToast('Match changed to ' + pick.title);
        return;
      }

      if (e.target && e.target.classList.contains('progress-input')) {
        const idx = parseInt(e.target.dataset.index, 10);
        const field = e.target.dataset.field;
        const item = currentAnimes[idx];
        if (!item || !field) return;
        const raw = e.target.value.trim();
        if (raw === '') {
          delete item[field];           // fall back to the derived value
        } else {
          let value = Math.max(0, Math.floor(Number(raw) || 0));
          const total = Number(item[PROGRESS_TOTAL_FIELD[field]] || 0);
          if (total > 0 && value > total) value = total;  // MAL rejects progress past the total
          item[field] = value;
          e.target.value = value;
        }
        updateStatsAndExport();
        return;
      }

      if (e.target && e.target.classList.contains('row-checkbox')) {
        const idx = parseInt(e.target.dataset.index, 10);
        if (!isNaN(idx) && currentAnimes[idx]) {
          currentAnimes[idx].enabled = e.target.checked;
          const tr = e.target.closest('tr');
          if (tr) {
            tr.classList.toggle('row-excluded', !e.target.checked);
          }
          updateSelectAllState();
          updateStatsAndExport();
        }
      }
    });

    // Allow clicking anywhere inside the checkbox cell to toggle
    tableBody.addEventListener('click', (e) => {
      const cell = e.target.closest('.cell-checkbox');
      if (cell && e.target.tagName !== 'INPUT') {
        const cb = cell.querySelector('.row-checkbox');
        if (cb) {
          cb.checked = !cb.checked;
          cb.dispatchEvent(new Event('change', { bubbles: true }));
        }
      }
    });

    btnCopyUnresolved.addEventListener('click', async () => {
      const missed = currentAnimes.filter(i => Number(i.id) <= 0).map(i => i.original || i.title);
      if (!missed.length) return;
      try {
        await navigator.clipboard.writeText(missed.join(String.fromCharCode(10)));
        showToast(`Copied ${missed.length} unmatched title(s)`);
      } catch (e) {
        showToast('Clipboard blocked - the unmatched rows are listed in the table', 'warning');
      }
    });

    searchInput.addEventListener('input', () => {
      renderTable(currentAnimes);
    });

    selectStatus.addEventListener('change', () => {
      const newStatus = selectStatus.value;
      currentAnimes.forEach(item => { item.status = newStatus; });
      renderTable(currentAnimes);
      if (currentAnimes.length > 0) updateStatsAndExport();
    });

    inputScore.addEventListener('input', () => {
      const newScore = parseInt(inputScore.value, 10) || 0;
      currentAnimes.forEach(item => { item.score = newScore; });
      if (currentAnimes.length > 0) updateStatsAndExport();
    });

    function escapeHtml(str) {
      if (!str) return '';
      return String(str)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;');
    }

    const btnTheme = document.getElementById('btnTheme');

    function applyTheme(mode) {
      document.body.classList.toggle('light', mode === 'light');
      btnTheme.textContent = mode === 'light' ? 'Dark mode' : 'Light mode';
    }

    let themeMode = null;
    try {
      themeMode = localStorage.getItem('aniexport.theme');
    } catch (e) {}
    if (themeMode !== 'light' && themeMode !== 'dark') themeMode = 'dark';
    applyTheme(themeMode);

    btnTheme.addEventListener('click', () => {
      themeMode = themeMode === 'light' ? 'dark' : 'light';
      try {
        localStorage.setItem('aniexport.theme', themeMode);
      } catch (e) {}
      applyTheme(themeMode);
    });

    // Initial load
    updateMediaTypeUI(false);
    loadData();
  </script>
</body>
</html>
"""


class MalGuiHandler(BaseHTTPRequestHandler):
    """HTTP Request Handler for the local MAL Export Studio Web GUI."""

    def log_message(self, format: str, *args: Any) -> None:
        # Suppress noisy standard request logging to console
        pass

    def send_json(self, data: Any, status: int = 200) -> None:
        payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path == "/" or path == "/index.html":
            body = HTML_TEMPLATE.encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        elif path == "/api/download/xml":
            if not EXPORT_XML_PATH.is_file():
                self.send_error(HTTPStatus.NOT_FOUND, "Export XML not found")
                return
            content = EXPORT_XML_PATH.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/xml; charset=utf-8")
            self.send_header("Content-Disposition", f'attachment; filename="{"myanimelist_manga_export.xml" if EXPORT_MEDIA_TYPE == "manga" else "myanimelist_export.xml"}"')
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
            return

        elif path == "/api/download/gz":
            if not EXPORT_GZ_PATH.is_file():
                self.send_error(HTTPStatus.NOT_FOUND, "Export GZ not found")
                return
            content = EXPORT_GZ_PATH.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/gzip")
            self.send_header("Content-Disposition", f'attachment; filename="{"myanimelist_manga_export.xml.gz" if EXPORT_MEDIA_TYPE == "manga" else "myanimelist_export.xml.gz"}"')
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
            return

        else:
            self.send_error(HTTPStatus.NOT_FOUND, "Not found")

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        content_length = int(self.headers.get("Content-Length", 0))
        raw_body = self.rfile.read(content_length) if content_length > 0 else b"{}"

        try:
            data = json.loads(raw_body.decode("utf-8"))
        except Exception:
            data = {}

        if path == "/api/process":
            global EXPORT_MEDIA_TYPE
            media_type = "manga" if data.get("media_type") == "manga" else "anime"
            EXPORT_MEDIA_TYPE = media_type
            content = data.get("content", "")
            default_status = data.get("status", "Completed")
            default_score = int(data.get("score", 0))

            # Parse lines from the pasted content directly (no disk reading)
            raw_lines = content.splitlines()
            cleaned_titles: List[str] = []
            for line in raw_lines:
                sanitized = sanitize_title_line(line)
                if sanitized:
                    cleaned_titles.append(sanitized)

            if not cleaned_titles:
                self.send_json({"ok": False, "error": f"No valid {media_type} titles found in the list."}, status=400)
                return

            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()

            def send_event(event_dict: Dict[str, Any]) -> None:
                payload = f"data: {json.dumps(event_dict, ensure_ascii=False)}\n\n".encode("utf-8")
                self.wfile.write(payload)
                self.wfile.flush()

            total_count = len(cleaned_titles)
            expand_sequels = bool(data.get("expand_sequels", False))
            session_cache: Dict[str, List[Dict[str, Any]]] = {}
            resolved_list: List[Dict[str, Any]] = []

            def notify_rate_limit(seconds: float) -> None:
                send_event({"type": "rate_limit", "seconds": round(seconds)})

            set_rate_limit_notifier(notify_rate_limit)
            for idx, raw_title in enumerate(cleaned_titles, start=1):
                send_event({
                    "type": "progress",
                    "current": idx,
                    "total": total_count,
                    "title": raw_title,
                    "percent": round(((idx - 1) / total_count) * 100, 1),
                    "interval": MIN_REQUEST_INTERVAL,
                })

                if media_type == "manga":
                    entries = resolve_manga_entries(raw_title, session_cache=session_cache, expand_sequels=expand_sequels)
                else:
                    entries = resolve_anime_entries(raw_title, session_cache=session_cache, expand_sequels=expand_sequels)
                for resolved in entries:
                    item = dict(resolved)
                    item["original"] = raw_title
                    item["status"] = default_status
                    item["score"] = default_score
                    resolved_list.append(item)

                primary_title = entries[0]["title"] if entries else raw_title
                send_event({
                    "type": "progress",
                    "current": idx,
                    "total": total_count,
                    "title": raw_title,
                    "resolved_title": primary_title,
                    "percent": round((idx / total_count) * 100, 1),
                })

            set_rate_limit_notifier(None)

            unique_entries = deduplicate_entries(resolved_list)

            # Build media-specific XML & GZ. Invalid MAL IDs are omitted by the builders.
            xml_str = (
                build_mal_manga_xml(unique_entries, default_status=default_status, default_score=default_score)
                if media_type == "manga"
                else build_mal_xml(unique_entries, default_status=default_status, default_score=default_score)
            )
            xml_bytes = xml_str.encode("utf-8")

            EXPORT_XML_PATH.write_bytes(xml_bytes)
            with open(EXPORT_GZ_PATH, "wb") as f_out:
                f_out.write(gzip.compress(xml_bytes, compresslevel=9))

            send_event({
                "type": "complete",
                "ok": True,
                "total": total_count,
                "entries": unique_entries,
            })
            self.close_connection = True
            return

        elif path == "/api/update-export":
            media_type = "manga" if data.get("media_type") == "manga" else "anime"
            EXPORT_MEDIA_TYPE = media_type
            entries = data.get("entries", [])
            default_status = data.get("status", "Completed")
            default_score = int(data.get("score", 0))

            if not entries:
                EXPORT_XML_PATH.unlink(missing_ok=True)
                EXPORT_GZ_PATH.unlink(missing_ok=True)
                self.send_json({"ok": True, "count": 0})
                return

            xml_str = (
                build_mal_manga_xml(entries, default_status=default_status, default_score=default_score)
                if media_type == "manga"
                else build_mal_xml(entries, default_status=default_status, default_score=default_score)
            )
            xml_bytes = xml_str.encode("utf-8")

            EXPORT_XML_PATH.write_bytes(xml_bytes)
            with open(EXPORT_GZ_PATH, "wb") as f_out:
                f_out.write(gzip.compress(xml_bytes, compresslevel=9))

            self.send_json({"ok": True, "count": len(entries)})
            return

        else:
            self.send_error(HTTPStatus.NOT_FOUND, "Not found")


def find_free_port(preferred_port: int = 5000) -> int:
    """Finds an open port starting from preferred_port."""
    for port in range(preferred_port, preferred_port + 50):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", port)) != 0:
                return port
    return preferred_port


def start_server(port: int = 5000, open_browser: bool = True) -> None:
    """Starts the local GUI web server and launches the browser."""
    actual_port = find_free_port(port)
    server_address = ("127.0.0.1", actual_port)
    httpd = ThreadingHTTPServer(server_address, MalGuiHandler)
    url = f"http://127.0.0.1:{actual_port}"

    print(f"\n==================================================")
    print(f"  AniExport - Local Server Running")
    print(f"==================================================")
    print(f"  URL: {url}")
    print(f"  Directory: {BASE_DIR}")
    print(f"  Press Ctrl+C in this console to stop.\n")

    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping local server...")
        httpd.server_close()
        print("Server stopped successfully.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Launch the local web GUI for AniExport."
    )
    parser.add_argument(
        "-p", "--port",
        type=int,
        default=5000,
        help="Local port for the web server (default: 5000).",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Do not automatically open the web browser on startup.",
    )

    args = parser.parse_args()
    start_server(port=args.port, open_browser=not args.no_browser)


if __name__ == "__main__":
    main()
