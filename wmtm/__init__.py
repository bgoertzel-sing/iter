"""Working Medium-Term Memory (WMTM) — mutable active layer between STM and LTM.

Modules:
  item:          WMTMItem dataclass
  attention:     ECAN AttentionValue with decay
  store:         WMTMStore bounded-capacity mutable store
  forgetting:    ForgettingPolicy for eviction decisions
  inference:     WMTMInferenceEngine (PLN deduction/induction/abduction)
  utility:       UtilityTracker for use/miss tracking
  forgetting_log: ForgettingLog to prevent re-derivation
  writeback:     WritebackManager for LTM promotion
  orchestrator:  WMTMOrchestrator tying it all together
  recall_bridge: Bridge from LTM journal to WMTM
"""
from .item import WMTMItem
from .attention import AttentionValue
from .store import WMTMStore
from .forgetting import ForgettingPolicy
from .inference import WMTMInferenceEngine, InferenceCandidate, BeliefTriple, extract_triples
from .utility import UtilityTracker, UtilityRecord
from .forgetting_log import ForgettingLog, ForgetRecord
from .writeback import WritebackManager, WritebackCandidate
from .orchestrator import WMTMOrchestrator, CycleResult
from .recall_bridge import RecallBridge, LTMCluster

__all__ = [
    "WMTMItem",
    "AttentionValue",
    "WMTMStore",
    "ForgettingPolicy",
    "WMTMInferenceEngine",
    "InferenceCandidate",
    "BeliefTriple",
    "extract_triples",
    "UtilityTracker",
    "UtilityRecord",
    "ForgettingLog",
    "ForgetRecord",
    "WritebackManager",
    "WritebackCandidate",
    "WMTMOrchestrator",
    "CycleResult",
    "RecallBridge",
    "LTMCluster",
]
