"""Offline tests: python -m unittest discover -s tests"""
import datetime as dt
import unittest

from sec_form4_clusters.scanner import find_clusters, init_db, parse_form4_xml, parse_index

XML = b"""<ownershipDocument><issuer><issuerName>Acme Corp</issuerName><issuerTradingSymbol>acme</issuerTradingSymbol></issuer>
<reportingOwner><reportingOwnerId><rptOwnerCik>123</rptOwnerCik><rptOwnerName>Doe Jane</rptOwnerName></reportingOwnerId>
<reportingOwnerRelationship><isDirector>1</isDirector><isOfficer>1</isOfficer><officerTitle>CEO</officerTitle></reportingOwnerRelationship></reportingOwner>
<nonDerivativeTable><nonDerivativeTransaction><transactionDate><value>2026-11-02</value></transactionDate>
<transactionCoding><transactionCode>P</transactionCode></transactionCoding>
<transactionAmounts><transactionShares><value>1,000</value></transactionShares><transactionPricePerShare><value>10.5</value></transactionPricePerShare></transactionAmounts>
</nonDerivativeTransaction></nonDerivativeTable></ownershipDocument>"""

IDX = """Form Type   Company Name   CIK   Date Filed   File Name
------------------------------------------------------------
4           ACME CORP                                                     1234567     20261102    edgar/data/1234567/0001234567-26-000001.txt
4/A         ACME CORP                                                     1234567     20261102    edgar/data/1234567/0001234567-26-000002.txt
8-K         OTHER INC                                                     7654321     20261102    edgar/data/7654321/0007654321-26-000003.txt
"""


class T(unittest.TestCase):
    def test_parse(self):
        p = parse_form4_xml(XML)
        self.assertEqual(p["ticker"], "ACME")
        self.assertEqual(p["transactions"][0]["dollar"], 10500.0)
        self.assertTrue(p["is_officer"] and p["is_director"])
        self.assertIsNone(parse_form4_xml(b"<nope"))

    def test_index_keeps_only_form4(self):
        rows = parse_index(IDX)
        self.assertEqual([r["accession"] for r in rows], ["0001234567-26-000001"])

    def test_cluster_gate(self):
        c = init_db(":memory:")
        c.execute("INSERT INTO filings (accession,cik,issuer_name,ticker,filed_date) VALUES ('a-1','0000000001','Acme','ACME','2026-11-03')")
        for i, cik in enumerate(["1", "2", "3"]):
            c.execute("INSERT INTO transactions (accession,owner_cik,owner_name,transaction_date,transaction_code,shares,price,dollar_value,footnote_10b5_1,is_director,is_officer,is_ten_pct) VALUES ('a-1',?,?,'2026-11-02','P',1,1,200000,0,1,0,0)", (cik, f"P{cik}"))
        c.commit()
        end = dt.date(2026, 11, 5)
        self.assertEqual(len(find_clusters(c, end)), 1)
        self.assertEqual(find_clusters(c, end, min_insiders=4), [])
        self.assertEqual(find_clusters(c, end, min_aggregate_usd=1_000_000), [])


if __name__ == "__main__":
    unittest.main()
