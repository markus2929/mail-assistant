from mailassistant.mail.mailboxes import mailbox_role, select_mailboxes, sync_priority
from mailassistant.mail.source import MailboxInfo

# Folder list as reported by the user's Proton Bridge (scripts/imap_probe.py).
RAW = {
    "All Mail": "\\All \\Marked \\Noinferiors", "Archive": "\\Archive \\Marked \\Noinferiors",
    "Drafts": "\\Drafts \\Marked \\Noinferiors", "Folders": "\\Noselect \\Unmarked",
    "Folders/Archives": "\\Marked", "Folders/Archives/2023": "\\Marked",
    "Folders/Bonnen": "\\Marked", "Folders/Login Codes": "\\Marked",
    "Folders/Unsubscribed mailing lists": "\\Marked", "Folders/[INBOX]Bestellingen": "\\Marked",
    "INBOX": "\\Marked \\Noinferiors", "Labels": "\\Noselect \\Unmarked",
    "Labels/Ads": "\\Marked", "Labels/ExampleBusiness": "\\Marked", "Labels/Persoonlijk": "\\Marked",
    "Sent": "\\Marked \\Noinferiors \\Sent", "Spam": "\\Junk \\Marked \\Noinferiors",
    "Starred": "\\Flagged \\Noinferiors \\Unmarked", "Trash": "\\Marked \\Noinferiors \\Trash",
}
INFOS = [MailboxInfo(n, frozenset(f.lower().split())) for n, f in RAW.items()]
INCLUDE, EXCLUDE = ["INBOX", "Sent", "Archive", "Folders/*"], ["Folders"]


def test_default_selection():
    names = {i.name for i in select_mailboxes(INFOS, INCLUDE, EXCLUDE)}
    assert names == {"INBOX", "Sent", "Archive", "Folders/Archives", "Folders/Archives/2023",
                     "Folders/Bonnen", "Folders/Login Codes", "Folders/Unsubscribed mailing lists",
                     "Folders/[INBOX]Bestellingen"}


def test_roles_and_priority():
    by = {i.name: i for i in INFOS}
    assert mailbox_role("INBOX") == "inbox"
    assert mailbox_role("Sent", by["Sent"].flags) == "sent"
    assert mailbox_role("Archive", by["Archive"].flags) == "archive"
    assert mailbox_role("Folders/Archives/2023") == "archive"
    assert mailbox_role("Folders/Bonnen") == "folder"
    assert mailbox_role("Labels/Ads") == "label"
    assert mailbox_role("Spam", by["Spam"].flags) == "spam"
    assert mailbox_role("Trash", by["Trash"].flags) == "trash"
    assert sync_priority(by["INBOX"]) < sync_priority(by["Sent"]) < sync_priority(by["Folders/Bonnen"])
