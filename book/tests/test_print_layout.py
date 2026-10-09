"""Long code tokens must stay inside the print trim."""

import sys
from pathlib import Path

import pdfplumber
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "build"))
from md import render_markdown  # noqa: E402
from pdf import render_pdf  # noqa: E402

BOOK = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("markdown", [
    '| Spring Boot | pyfly | Notes |\n|---|---|---|\n'
    '| `JpaRepository<E, ID>` | `Repository[WalletEntity, str]` | '
    '`from pyfly.data.relational.sqlalchemy import Repository`; every repository joins the transaction. |\n'
    '| `@Repository interface WalletRepo extends JpaRepository<…>` | '
    '`@repository class WalletRepository(Repository[WalletEntity, str])` | '
    'Class, not interface; body holds derived-query stubs and custom methods only. |\n',
    'How it works: Each `.step(step_id)` call returns a `StepBuilder`. Chain configuration methods — '
    '`.handler()`, `.compensate()`, `.depends_on()`, `.retry()`, `.backoff_ms()`, `.timeout_ms()`, `.jitter()` '
    '— then call `.add()` to finalise the step and return the parent `SagaBuilder`.\n',
    '```\nlumen_deposit_duration_seconds_bucket{class="DepositFundsHandler",method="do_handle",'
    'exception="none",le="0.05"} 1.0\n```\n',
], ids=['wide-table', 'inline-code-chain', 'metric-labels'])
def test_long_code_stays_within_pdf_page(tmp_path, markdown):
    target = tmp_path / 'layout.pdf'
    body = render_markdown(markdown, BOOK)
    render_pdf('<!doctype html><html><body>' + body + '</body></html>', BOOK,
               [BOOK / 'theme' / name for name in ('tokens.css', 'pygments.css', 'book.css', 'print.css')],
               target)
    with pdfplumber.open(target) as pdf:
        for page in pdf.pages:
            outside = [char['text'] for char in page.chars if char['text'].strip() and (
                char['x0'] < -1 or char['top'] < -1
                or char['x1'] > page.width + 1 or char['bottom'] > page.height + 1
            )]
            assert not outside, ''.join(outside)


def test_long_table_begins_beside_its_heading(tmp_path):
    markdown = '### Repository reference\n\n| Operation | Notes |\n|---|---|\n'
    for number in range(12):
        markdown += f'| Entry{number} | ' + 'A repository operation joins the current transaction. ' * 4 + '|\n'
    target = tmp_path / 'table.pdf'
    render_pdf('<!doctype html><html><body>' + render_markdown(markdown, BOOK) + '</body></html>', BOOK,
               [BOOK / 'theme' / name for name in ('tokens.css', 'pygments.css', 'book.css', 'print.css')],
               target)
    with pdfplumber.open(target) as pdf:
        first = pdf.pages[0].extract_text()
        assert 'Repository reference' in first
        assert 'Entry0' in first
