import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import fetch_registry as fr  # noqa: E402

# Caixa auction property, consolidated to Caixa, no open lien. Reproduces the OCR quirks of a real
# certificate: the hyphen dropped in the act header ("R.3M." instead of "R. 3-M.") and margin noise
# ("> ", "E ") glued to the left of lines. No real names, CPF, RG or address.
CONSOLIDATED = """
E AV. 1-M. 24.726, em 13 de setembro de 2011.
ABERTURA DE MATRÍCULA - regularização fundiária de interesse social desta serventia.
R. 2-M. 24.726, em 24 de agosto de 2023.
COMPRA E VENDA - os proprietários venderam o imóvel ao adquirente, pelo valor ajustado.
> R.3M., 24.726, em 23 de outubro de 2023.
ALIENAÇÃO FIDUCIÁRIA - o adquirente alienou fiduciariamente o imóvel à Caixa Econômica Federal.
R. 4-M. 24.726, em 23 de outubro de 2023.
ALIENAÇÃO FIDUCIÁRIA - garantia do financiamento concedido pelo Sistema Financeiro da Habitação.
AV. 5-M. 24.726, em 23 de outubro de 2023.
CÉDULA DE CRÉDITO IMOBILIÁRIO - emitida pela credora fiduciária Caixa Econômica Federal.
AV. 6-M. 24.726, em 1º de abril de 2026.
CONSOLIDAÇÃO DA PROPRIEDADE - fica consolidada a propriedade em nome da Caixa Econômica Federal.
AV. 7-M. 24.726, em 1º de abril de 2026.
CANCELAMENTO - faço esta averbação para cancelar a AV. 5 desta matrícula, face a consolidação.
"""

# Hipoteca (R.4) cancelled later (AV.7); penhora (AV.5) and indisponibilidade (R.6) left open.
WITH_LIENS = """
R. 4-M. 12.345, em 10 de janeiro de 2020.
HIPOTECA - o imóvel foi dado em hipoteca a favor do Banco X para garantia de dívida.
AV. 5-M. 12.345, em 5 de março de 2021.
PENHORA - nos autos da ação de execução n. 123, foi penhorado o imóvel.
R. 6-M. 12.345, em 8 de agosto de 2022.
INDISPONIBILIDADE - averbada ordem de indisponibilidade (CNIB) sobre o imóvel.
AV. 7-M. 12.345, em 1 de abril de 2024.
CANCELAMENTO - faço esta averbação para cancelar a R. 4 desta matrícula, baixada a hipoteca.
"""


class ParseTest(unittest.TestCase):
    def test_reads_every_act_even_when_ocr_drops_the_hyphen(self):
        refs = [a["ref"] for a in fr.parse(CONSOLIDATED)]
        # "R.3M." lost its hyphen in OCR; the tolerant header still captures it.
        self.assertEqual(refs, ["AV.1", "R.2", "R.3", "R.4", "AV.5", "AV.6", "AV.7"])

    def test_cancellation_act_is_not_itself_a_lien(self):
        acts = {a["ref"]: a for a in fr.parse(WITH_LIENS)}
        self.assertTrue(acts["R.4"]["isOnus"])        # hipoteca
        self.assertFalse(acts["AV.7"]["isOnus"])      # cancela a hipoteca, não é ônus
        self.assertEqual(acts["AV.7"]["cancels"], [4])


class AssessTest(unittest.TestCase):
    def test_consolidated_caixa_property_is_ok(self):
        result = fr.assess(fr.parse(CONSOLIDATED))
        self.assertEqual(result["risk"], "ok")
        self.assertTrue(result["consolidated"])
        self.assertEqual(result["openOnus"], [])

    def test_open_liens_raise_an_alert(self):
        result = fr.assess(fr.parse(WITH_LIENS))
        self.assertEqual(result["risk"], "alerta")
        refs = {o["ref"] for o in result["openOnus"]}
        self.assertEqual(refs, {"AV.5", "R.6"})       # penhora e indisponibilidade; hipoteca saiu

    def test_empty_text_is_indeterminate(self):
        result = fr.assess(fr.parse(""))
        self.assertEqual(result["risk"], "indeterminado")


class PlanTest(unittest.TestCase):
    def test_new_then_changed_then_stale(self):
        now = datetime(2026, 11, 1, tzinfo=timezone.utc)
        old = (now - timedelta(days=40)).isoformat(timespec="seconds")
        fresh = now.isoformat(timespec="seconds")
        listing = {"a": ("100", "Leilão"), "b": ("200", "Venda Online"),
                   "c": ("300", "Leilão"), "d": ("400", "Leilão")}
        cache = {
            "b": {"price": "200", "modality": "Venda Online", "fetchedAt": fresh},  # igual e fresco
            "c": {"price": "999", "modality": "Leilão", "fetchedAt": fresh},         # preço mudou
            "d": {"price": "400", "modality": "Leilão", "fetchedAt": old},           # velho
        }
        todo = fr.plan(listing, cache, now, max_fetch=10, stale_after=timedelta(days=30))
        self.assertEqual(todo, ["a", "c", "d"])       # novo, alterado, velho; 'b' fica de fora


if __name__ == "__main__":
    unittest.main()
