import json
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import fetch_details as fd  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def page(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as source:
        return fd.parse_detail(source.read())


class ParseDetailTest(unittest.TestCase):
    def test_leilao_sfi(self):
        d = page("detail-leilao-sfi.html")
        self.assertEqual(d["registration"], "133654")
        self.assertEqual(d["county"], "BAURU-SP")
        self.assertEqual(d["municipalRegistration"], "000")
        self.assertEqual(d["registrationUrl"], "/editais/matricula/SP/8444426427425.pdf")
        self.assertEqual(d["editalUrl"], "/editais/EL00570226CPARE.PDF")
        self.assertEqual(d["auctioneerSite"], "www.lancecertoleiloes.com.br")
        self.assertEqual(d["auctionDates"], ["Data do 1º Leilão - 06/01/2027 - 10h00", "Data do 2º Leilão - 12/01/2027 - 10h00"])
        self.assertEqual(d["payments"][0], "Recursos próprios.")
        self.assertEqual(d["expenses"], ["Condomínio: Sob responsabilidade do comprador.", "Tributos: Sob responsabilidade do comprador."])
        self.assertIn("CEP: 17047-450", d["address"])
        self.assertEqual(d["description"], "Quarto, 1 Vaga na Garagem, Wc, Sala, Cozinha.")
        self.assertNotIn("occupancy", d)

    def test_venda_direta_has_expense_limits(self):
        d = page("detail-venda-direta.html")
        self.assertEqual(d["registration"], "24726")
        self.assertEqual(d["municipalRegistration"], "1227000")
        self.assertEqual(d["landArea"], "224,29m2")
        self.assertTrue(d["expenses"][0].startswith("Condomínio: Sob responsabilidade do comprador, até o limite de 10%"))
        self.assertNotIn("editalUrl", d)

    def test_licitacao_aberta_date(self):
        d = page("detail-licitacao-aberta.html")
        self.assertEqual(d["auctionDates"], ["Data da Licitação Aberta - 11/11/2026 - 10h00"])
        self.assertEqual(d["payments"], ["Exclusivamente à vista (somente recursos próprios)."])

    def test_unavailable(self):
        self.assertEqual(page("detail-unavailable.html"), {"unavailable": True})

    def test_challenge_page_is_not_a_detail_page(self):
        self.assertIsNone(fd.parse_detail("<title>Radware Bot Manager CAPTCHA</title>"))


class OccupancyTest(unittest.TestCase):
    def test_words(self):
        self.assertEqual(fd.occupancy("Imóvel ocupado por terceiros"), "OCUPADO")
        self.assertEqual(fd.occupancy("Imóvel desocupado"), "DESOCUPADO")
        self.assertEqual(fd.occupancy("O imóvel não está ocupado"), "DESOCUPADO")
        self.assertIsNone(fd.occupancy("Casa, 2 quartos"))


class PlanTest(unittest.TestCase):
    now = datetime(2026, 10, 31, tzinfo=timezone.utc)
    week = timedelta(days=7)

    def test_new_then_changed_then_stale(self):
        listing = {"1": ("10", "A"), "2": ("20", "A"), "3": ("30", "A"), "4": ("40", "A")}
        fresh = (self.now - timedelta(days=1)).isoformat()
        old = (self.now - timedelta(days=9)).isoformat()
        cache = {"2": {"price": "99", "modality": "A", "fetchedAt": fresh},
                 "3": {"price": "30", "modality": "A", "fetchedAt": old},
                 "4": {"price": "40", "modality": "A", "fetchedAt": fresh}}
        self.assertEqual(fd.plan(listing, cache, self.now, 10, self.week), ["1", "2", "3"])

    def test_cap(self):
        listing = {str(i): ("1", "A") for i in range(5)}
        self.assertEqual(len(fd.plan(listing, {}, self.now, 2, self.week)), 2)


if __name__ == "__main__":
    unittest.main()
