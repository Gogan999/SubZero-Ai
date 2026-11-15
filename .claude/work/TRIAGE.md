# Test triage

Counts are `def test_` methods per file (inheritance is not multiplied), from `git show HEAD:<file>` against the working
tree after F3a. Files not listed were not touched (or only had a dead form key removed, see the last row).

## F3a (projects, share links, artifacts, explainers, Drive, uploads, composer tools)

| File | Before | After | Deleted (and why) | Rewritten / added |
|---|---|---|---|---|
| `tests/test_features.py` | 34 | 12 | `ArtifactsTest` (5: gallery, artifact page, titles; artifacts removed), `ProjectsTest` (6: projects removed), `ShareTest` (5: share links removed), `ComposerToolsTest` tools / Research / daily-search-limit tests (3: per-message tools removed), `ConnectorsTest` menu / browse / GitHub-file-attached / Drive links (4: chat file picker and Drive removed) | Kept: Profile (2; theme `next` now `/profile`), `CodeTest` (7 unchanged), `test_admin_sets_up_github` (no Drive field), `test_plain_messages_use_no_tools`, `test_stream_parser_reads_searches_and_sources` (class renamed `RepliesWithoutToolsTest`). New: `test_old_tool_fields_in_a_request_are_ignored` |
| `tests/test_milestone3.py` | 31 | 12 | `ExplainerRenderTest` frame / numbering / base / oversize / download tests (5), `PrepareTest` (6: `prepare` removed), `ContextTest` file-block tests (2), explainer flow tests (3: card, frame lockdown, access), upload flow tests (8: approve then reach Claude, rejected uploads, videos, stills, old pictures, edited keeps files, composer offers files, asking in words) | Kept: diffs highlighted, `SearchTest`, `MigrationTest`, everyday effort, oversized request 413, long-chat notice. Rewritten: `ContextTest` (old files become `[N attached file(s) not shown]`), files-visible-only-with-their-message (rows inserted directly, `/files/{aid}` still serves old files). New: explainer fence is an ordinary code block; chat with old attachment and project rows still gets a reply and the history note; uploads refused with a plain message. `png_bytes` stays here only as a small helper |
| `tests/test_capabilities.py` | 14 | 6 | `ComposerTest` (3), `RefusalTest` (3), `ModelSwitchTest` (2): all test the tools menu, upload refusals or dropped tools per message | Kept `MatrixTest` (3) and `CodeModelTest` (3) |
| `tests/test_approval_window.py` | 25 | 24 | `test_messages_with_files_always_wait` (files can't be sent any more; the upload refusal is covered in `test_milestone3`) | none; the other window tests are unchanged |
| `tests/test_chat_tools.py` | 17 | 17 | none (despite its name it has no composer-tool tests: scrolling chat, admin delete, pricing, cost shown) | `DeleteChatTest` now inserts the old attachment row directly instead of uploading; share-row test renamed (old `shares` rows are still deleted with the chat) |
| `tests/test_milestone4.py` | 49 | 49 | none | `BackupTest` and `NightlyTest` insert old attachment/video rows directly (backups and clean-up still handle old files); removed `max_tokens_explainer` from the validation test and form |
| `tests/test_model_check.py` | 36 | 36 | `test_no_explainer_fails` (case removed from the check) | case counts 7 to 6 and 5 to 4; new `test_a_pass_saved_with_the_old_explainer_case_still_counts` (CHECK_VERSION stays 1) |
| `tests/test_slack_dm.py` | 50 | 50 | `test_replace_explainers`, `test_explainer_reply_links_to_the_website` (website link gone) | New `test_flatten_explainers` and `test_a_reply_with_an_explainer_fence_posts_as_code_without_a_website_link` (scripted reply through the whole Slack DM flow); `reply_chunks` lost its link argument |
| `tests/test_flow.py` | 47 | 47 | none | `test_sidebar_links`: no Projects/Artifacts links, those addresses are 404 for now, no Coming soon pages |
| `tests/test_settings_dashboard.py` | 30 | 30 | none | `max_text_file_kb` removed from the Site form, live-setting and bad-value tests and from the "every setting has a place" list; `connector_drive_links` removed there too |
| `tests/test_providers.py`, `tests/test_model_picker.py`, `tests/test_claude_bridge.py` | 23 / 12 / 25 | same | none | dead `max_tokens_explainer` key removed from posted forms |
| `tests/helpers.py` | n/a | n/a | n/a | New `Harness.add_old_attachment` and `Harness.add_old_project` (insert rows the way 0.10 wrote them) |
| `tests/test_removed_features.py` (new) | 0 | 7 | n/a | `/s/...` gives 410 with the message, no sign-in, no lookup (even for a real old link row); removed pages (`/projects`, `/artifacts`, `/connectors/github`, share and explainer addresses) are 404; no removed links, tools or file picker on the pages; page smoke test by role (`/chats`, a chat, `/queue`, `/code`, `/settings`, `/audit`, `/profile`, `/about`); removed settings in `config.env` and the settings table plus an unknown key are ignored at start-up; Settings page and validation no longer know them |

`tests/test_rules.py` is unchanged and passes. Rules 1 to 11 keep their tests (none of the deleted tests was the only cover for a rule).

## Still to do in F3b

Web-chat tests (`test_flow.py`, `test_features.py` Profile, `test_approval_window.py`, `test_chat_tools.py`, parts of `test_milestone3.py` and `test_milestone4.py`) still drive `/chats/new` and `/c/{id}/messages`; F3b decides keep, rewrite through Slack, or delete for each.
