"""Full-corpus production verification and sign-off (roadmap #15).

corpus
    Corpus: the documents under verification, pinned by content (stele.corpus v1).
campaign
    Campaign: every document through a baseline lane (today's production
    setup) and candidate lanes, sealed in the ledger, journaled resumably.
report
    build_report: re-checks the campaign against the ledger, archive and
    corpus, compares candidates with the baseline, and decides the gates.
signoff
    sign_off / check_sign_off: a named person's acceptance of a passed report,
    anchored in the hash-chained event log; stale once a record it covers is
    invalidated.
lanes
    Lanes for script parsers, packaged ML parsers and Wasm extractors.

CLI: python -m stele.verification {corpus,run,report,sign-off,check,list}.
"""
from .campaign import Campaign, CampaignError, Journal, Lane, Observation
from .corpus import Corpus, CorpusEntry, CorpusFormatError
from .report import Gates, ReportFormatError, VerificationReport, Waiver, build_report
from .signoff import SIGNOFF_EVENT, SignOff, SignOffRefused, check_sign_off, sign_off, sign_offs

__all__ = [
    "Campaign", "CampaignError", "Journal", "Lane", "Observation",
    "Corpus", "CorpusEntry", "CorpusFormatError",
    "Gates", "ReportFormatError", "VerificationReport", "Waiver", "build_report",
    "SIGNOFF_EVENT", "SignOff", "SignOffRefused", "check_sign_off", "sign_off", "sign_offs",
]
