"""Unit tests for AttentionValue (ECAN attention triple with decay)."""
import pytest
from wmtm.attention import AttentionValue


class TestAttentionValueInit:
    def test_defaults_zero(self):
        av = AttentionValue()
        assert av.sti == 0.0
        assert av.ati == 0.0
        assert av.lti == 0.0

    def test_custom_values(self):
        av = AttentionValue(sti=10.0, ati=5.0, lti=2.0)
        assert av.sti == 10.0
        assert av.ati == 5.0
        assert av.lti == 2.0


class TestAttentionValueTick:
    def test_sti_decays(self):
        av = AttentionValue(sti=1.0)
        av.tick()
        assert av.sti == pytest.approx(0.9)

    def test_ati_decays(self):
        av = AttentionValue(ati=1.0)
        av.tick()
        assert av.ati == pytest.approx(0.97)

    def test_lti_decays(self):
        av = AttentionValue(lti=1.0)
        av.tick()
        assert av.lti == pytest.approx(0.995)

    def test_multiple_ticks(self):
        av = AttentionValue(sti=100.0)
        for _ in range(5):
            av.tick()
        # 100 * 0.9^5 = 100 * 0.59049
        assert av.sti == pytest.approx(59.049, abs=0.01)

    def test_zero_stays_zero(self):
        av = AttentionValue()
        av.tick()
        assert av.sti == 0.0
        assert av.ati == 0.0
        assert av.lti == 0.0

    def test_all_three_decay_independently(self):
        av = AttentionValue(sti=10.0, ati=8.0, lti=6.0)
        av.tick()
        assert av.sti == pytest.approx(9.0)
        assert av.ati == pytest.approx(7.76)
        assert av.lti == pytest.approx(5.97)


class TestAttentionValueBoost:
    def test_boost_adds_to_sti(self):
        av = AttentionValue(sti=5.0)
        av.boost(3.0)
        assert av.sti == pytest.approx(8.0)

    def test_boost_zero(self):
        av = AttentionValue(sti=5.0)
        av.boost(0.0)
        assert av.sti == 5.0

    def test_boost_negative(self):
        av = AttentionValue(sti=5.0)
        av.boost(-2.0)
        assert av.sti == pytest.approx(3.0)


class TestAttentionValuePenalty:
    def test_penalty_reduces_sti(self):
        av = AttentionValue(sti=10.0)
        av.penalty(3.0)
        assert av.sti == pytest.approx(7.0)

    def test_penalty_clamps_to_zero(self):
        av = AttentionValue(sti=2.0)
        av.penalty(10.0)
        assert av.sti == 0.0

    def test_penalty_zero_sti_stays_zero(self):
        av = AttentionValue(sti=0.0)
        av.penalty(5.0)
        assert av.sti == 0.0


class TestAttentionValueTotal:
    def test_weighted_total(self):
        av = AttentionValue(sti=10.0, ati=4.0, lti=5.0)
        # 10*1.0 + 4*0.5 + 5*0.2 = 10 + 2 + 1 = 13
        assert av.total == pytest.approx(13.0)

    def test_total_zero(self):
        av = AttentionValue()
        assert av.total == 0.0

    def test_total_only_sti(self):
        av = AttentionValue(sti=10.0)
        assert av.total == pytest.approx(10.0)

    def test_total_only_ati(self):
        av = AttentionValue(ati=10.0)
        # 10 * 0.5 = 5.0
        assert av.total == pytest.approx(5.0)

    def test_total_only_lti(self):
        av = AttentionValue(lti=10.0)
        # 10 * 0.2 = 2.0
        assert av.total == pytest.approx(2.0)


class TestAttentionValueDecayRates:
    def test_custom_decay_rates(self):
        av = AttentionValue(sti=100.0, sti_decay=0.5)
        av.tick()
        assert av.sti == pytest.approx(50.0)

    def test_decay_rate_one_means_no_decay(self):
        av = AttentionValue(sti=10.0, sti_decay=1.0, ati_decay=1.0, lti_decay=1.0)
        av.tick()
        assert av.sti == 10.0
        assert av.ati == 0.0
        assert av.lti == 0.0
