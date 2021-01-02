# Copyright 2012-2018 Camptocamp SA
# Copyright 2020 CorporateHub (https://corporatehub.eu)
# Copyright 2020 Tecnativa - Pedro M. Baeza
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).

from datetime import timedelta

from odoo import exceptions, fields
from odoo.tests import common


class TestCurrencyRevaluation(common.SavepointCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        cls.today = fields.Date.today()
        cls.company = cls.env.ref("account_multicurrency_revaluation.res_company_reval")
        cls.env.user.write({"company_ids": [(4, cls.company.id, False)]})
        cls.env.user.company_id = cls.company
        cls.reval_journal = cls.env.ref(
            "account_multicurrency_revaluation.reval_journal"
        )
        cls.company.write(
            {
                "revaluation_loss_account_id": cls.env.ref(
                    "account_multicurrency_revaluation.acc_reval_loss"
                ).id,
                "revaluation_gain_account_id": cls.env.ref(
                    "account_multicurrency_revaluation.acc_reval_gain"
                ).id,
                "currency_reval_journal_id": cls.reval_journal.id,
            }
        )
        cls.receivable_acc = cls.env["account.account"].create(
            {
                "name": "Account Receivable",
                "code": "accrec",
                "user_type_id": cls.env.ref("account.data_account_type_receivable").id,
                "currency_revaluation": True,
                "reconcile": True,
                "company_id": cls.company.id,
            }
        )
        payable_acc = cls.env.ref("account_multicurrency_revaluation.demo_acc_payable")
        cls.partner = cls.env.ref("base.res_partner_3")
        cls.partner.company_id = cls.company.id
        cls.partner.property_account_payable_id = payable_acc.id
        cls.partner.property_account_receivable_id = cls.receivable_acc.id

    def test_defaults(self):
        # TODO: This causes that the environment to be reset and screw up tests,
        # so disabled for now
        self.env["res.config.settings"].create(
            {
                "default_currency_reval_journal_id": self.reval_journal.id,
                "revaluation_loss_account_id": self.env.ref(
                    "account_multicurrency_revaluation.acc_reval_loss"
                ).id,
                "revaluation_gain_account_id": self.env.ref(
                    "account_multicurrency_revaluation.acc_reval_gain"
                ).id,
            }
        ).execute()

        wizard = self.env["wizard.currency.revaluation"].create({})

        self.assertEqual(wizard.revaluation_date, fields.Date.today())
        self.assertEqual(wizard.journal_id, self.reval_journal)

    def test_revaluation(self):
        """Create Invoices and Run the revaluation currency wizard
        with different rates which result should be:
                                 Debit    Credit    Amount Currency
        Customer Invoice US      25.00    0.00      100.00
        Customer Invoice US      40.00    0.00      100.00

        Fisrt wizard execution:

            Currency Reval. 1.0  135.00   0.00    0.00

        Second wizard execution:

            Currency Reval 1.25    0.00   40.00     0.00
        """
        self.delete_journal_data()
        usd_currency = self.env.ref("base.USD")
        rates = {
            (self.today - timedelta(days=30)): 4.00,
            (self.today - timedelta(days=15)): 2.50,
            (self.today - timedelta(days=7)): 1.00,
            (self.today - timedelta(days=1)): 1.25,
        }
        self.create_rates(rates, usd_currency)
        values = {
            "reversable_revaluations": False,
        }
        self.company.write(values)

        invoice1 = self.create_invoice(
            self.today - timedelta(days=30), usd_currency, 1.0, 100.00
        )
        invoice1.post()
        invoice2 = self.create_invoice(
            self.today - timedelta(days=15), usd_currency, 1.0, 100.00
        )
        invoice2.post()
        reval_move_lines = self.env["account.move.line"].search(
            [("account_id", "=", self.receivable_acc.id)]
        )
        self.assertEqual(sum(reval_move_lines.mapped("debit")), 65.00)
        self.assertEqual(sum(reval_move_lines.mapped("amount_currency")), 200.00)

        result = self.wizard_execute(self.today - timedelta(days=7))
        self.assertEqual(result.get("name"), "Created revaluation lines")
        reval_move_lines = self.env["account.move.line"].search(
            [("account_id", "=", self.receivable_acc.id)]
        )
        self.assertEqual(sum(reval_move_lines.mapped("debit")), 200.00)
        self.assertEqual(sum(reval_move_lines.mapped("amount_currency")), 200.00)

        result = self.wizard_execute(self.today - timedelta(days=1))
        self.assertEqual(result.get("name"), "Created revaluation lines")
        reval_move_lines = self.env["account.move.line"].search(
            [("account_id", "=", self.receivable_acc.id)]
        )
        self.assertEqual(sum(reval_move_lines.mapped("debit")), 200.00)
        self.assertEqual(sum(reval_move_lines.mapped("credit")), 40.00)
        self.assertEqual(sum(reval_move_lines.mapped("amount_currency")), 200.00)

        wizard = self.env["unrealized.report.printer"]
        wiz = wizard.create({})
        result = wiz.print_report()
        account_ids = result.get("context").get("active_ids")
        report = (
            self.env["account.move.line"]
            .search([("account_id", "in", account_ids)])
            .filtered(lambda l: l.account_id.code == "accrec")
        )
        self.assertEqual(sum(report.mapped("debit")), 200)
        self.assertEqual(sum(report.mapped("credit")), 40)
        self.assertEqual(sum(report.mapped("amount_currency")), 200)

    def test_revaluation_payment(self):
        """Create an Invoice and execute the revaluation currency wizard with
        a rate of 0.75:
                                    Debit    Credit       Amount Currency
        Account Receivable
          currency revaluation      0.00     2666.67      0.00
          customer invoices         6666.67  0.00         5000.00

        Make a payment with 1.0 rate and execute the revaluation currency
        wizard with a rate of 1.25:

                                    Debit     Credit      Amount Currency
        Account Receivable
          currency revaluation      0.00      2666.67     0.00
          currency revaluation      0.00      800.00      0.00
          euro bank                 0.00      4000.00    -4000.00
          customer invoices         6666.67   0.00        5000.00

        """
        self.delete_journal_data()
        usd_currency = self.env.ref("base.USD")
        eur_currency = self.env.ref("base.EUR")
        self.company.currency_id = usd_currency.id
        rates = {
            (self.today - timedelta(days=90)): 0.75,
            (self.today - timedelta(days=80)): 1.00,
            (self.today - timedelta(days=70)): 1.25,
        }
        self.create_rates(rates, eur_currency)
        self.create_rates({fields.Date.to_date("2001-01-01"): 1}, usd_currency)
        values = {
            "reversable_revaluations": False,
            "currency_id": usd_currency.id,
        }
        self.company.write(values)

        invoice = self.create_invoice(
            self.today - timedelta(days=89), eur_currency, 5.0, 1000.00
        )
        invoice.post()
        result = self.wizard_execute(self.today - timedelta(days=70))
        self.assertEqual(result.get("name"), "Created revaluation lines")
        reval_move_lines = self.env["account.move.line"].search(
            [("account_id", "=", self.receivable_acc.id)]
        )
        self.assertAlmostEqual(sum(reval_move_lines.mapped("debit")), 6666.67)
        self.assertAlmostEqual(sum(reval_move_lines.mapped("credit")), 2666.67)
        self.assertAlmostEqual(sum(reval_move_lines.mapped("amount_currency")), 5000.00)

        euro_bank = self.env["account.journal"].create(
            {
                "name": "Euro Bank",
                "type": "bank",
                "code": "EURBK",
                "currency_id": eur_currency.id,
            }
        )
        euro_bank.default_debit_account_id.currency_revaluation = True
        payment_method = self.env.ref(
            "account_multicurrency_revaluation.account_payment_method_manual_in"
        )

        # Register partial payment
        payment = self.env["account.payment"].create(
            {
                "invoice_ids": [(4, invoice.id, 0)],
                "amount": 4000,
                "currency_id": eur_currency.id,
                "payment_date": self.today - timedelta(days=79),
                "communication": "Invoice partial payment",
                "partner_id": invoice.partner_id.id,
                "partner_type": "customer",
                "journal_id": euro_bank.id,
                "payment_type": "inbound",
                "payment_method_id": payment_method.id,
                "payment_difference_handling": "open",
                "writeoff_account_id": False,
            }
        )
        payment.post()

        result = self.wizard_execute(self.today - timedelta(days=70))
        self.assertEqual(result.get("name"), "Created revaluation lines")
        reval_move_lines = self.env["account.move.line"].search(
            [("account_id", "=", self.receivable_acc.id)]
        )
        self.assertAlmostEqual(sum(reval_move_lines.mapped("debit")), 7466.67)
        self.assertAlmostEqual(sum(reval_move_lines.mapped("credit")), 6666.67)
        self.assertAlmostEqual(sum(reval_move_lines.mapped("amount_currency")), 1000.00)

        receivable_lines = len(reval_move_lines)
        with self.assertRaises(exceptions.Warning):
            self.wizard_execute(self.today - timedelta(days=70))
        reval_move_lines = self.env["account.move.line"].search(
            [("account_id", "=", self.receivable_acc.id)]
        )
        self.assertEqual(len(reval_move_lines), receivable_lines)

    def test_revaluation_bank_account(self):
        self.delete_journal_data()
        usd_currency = self.env.ref("base.USD")
        eur_currency = self.env.ref("base.EUR")
        rates = {
            (self.today - timedelta(days=90)): 0.75,
            (self.today - timedelta(days=80)): 1.00,
            (self.today - timedelta(days=70)): 1.25,
        }
        self.create_rates(rates, eur_currency)
        values = {
            "reversable_revaluations": False,
            "currency_id": usd_currency.id,
        }
        self.company.write(values)

        eur_bank = self.env["account.journal"].create(
            {
                "name": "EUR Bank",
                "type": "bank",
                "code": "EURBK",
                "currency_id": eur_currency.id,
            }
        )
        eur_bank.default_debit_account_id.currency_revaluation = True
        bank_account = eur_bank.default_debit_account_id

        liability_account = self.env["account.account"].create(
            {
                "name": "Liability",
                "code": "L",
                "user_type_id": self.env.ref(
                    "account.data_account_type_current_liabilities"
                ).id,
                "company_id": self.company.id,
            }
        )

        bank_stmt = self.env["account.bank.statement"].create(
            {
                "journal_id": eur_bank.id,
                "date": self.today - timedelta(days=60),
                "name": "Statement",
            }
        )

        bank_stmt_line_1 = self.env["account.bank.statement.line"].create(
            {
                "name": "Incoming 100 EUR",
                "statement_id": bank_stmt.id,
                "amount": 100.0,
                "date": self.today - timedelta(days=90),
            }
        )
        bank_stmt_line_1.process_reconciliation(
            new_aml_dicts=[
                {
                    "credit": 100.0,
                    "debit": 0.0,
                    "name": "Incoming 100 EUR",
                    "account_id": liability_account.id,
                }
            ]
        )

        bank_stmt_line_2 = self.env["account.bank.statement.line"].create(
            {
                "name": "Outgoing 25 EUR",
                "statement_id": bank_stmt.id,
                "amount": -25.0,
                "date": self.today - timedelta(days=79),
            }
        )
        invoice = self.create_invoice(
            self.today - timedelta(days=79), eur_currency, 1.0, 25.0
        )
        invoice.post()
        invoice_move_line = next(
            move_line
            for move_line in invoice.line_ids
            if move_line.account_id == self.receivable_acc
        )
        bank_stmt_line_2.process_reconciliation(
            counterpart_aml_dicts=[
                {
                    "move_line": invoice_move_line,
                    "credit": 0,
                    "debit": 25,
                    "name": invoice_move_line.name,
                }
            ]
        )

        bank_stmt_line_3 = self.env["account.bank.statement.line"].create(
            {
                "name": "Incoming 50 EUR",
                "statement_id": bank_stmt.id,
                "amount": 50.0,
                "date": self.today - timedelta(days=69),
            }
        )
        bank_stmt_line_3.process_reconciliation(
            new_aml_dicts=[
                {
                    "credit": 50.0,
                    "debit": 0.0,
                    "name": "Incoming 50 EUR",
                    "account_id": liability_account.id,
                }
            ]
        )

        bank_account_lines = self.env["account.move.line"].search(
            [("account_id", "=", bank_account.id)]
        )
        self.assertEqual(len(bank_account_lines), 3)
        self.assertEqual(sum(bank_account_lines.mapped("debit")), 173.33)
        self.assertEqual(sum(bank_account_lines.mapped("credit")), 25.0)
        self.assertEqual(sum(bank_account_lines.mapped("amount_currency")), 125.0)

        result = self.wizard_execute(self.today - timedelta(days=60))
        self.assertEqual(result.get("name"), "Created revaluation lines")

        new_bank_account_lines = self.env["account.move.line"].search(
            [("account_id", "=", bank_account.id)]
        )
        revaluation_line = new_bank_account_lines - bank_account_lines
        self.assertEqual(len(revaluation_line), 1)
        self.assertEqual(revaluation_line.debit, 0.0)
        self.assertEqual(revaluation_line.credit, 48.33)
        self.assertEqual(revaluation_line.amount_currency, 0.0)

        bank_account_lines |= revaluation_line
        self.assertEqual(sum(bank_account_lines.mapped("debit")), 173.33)
        self.assertEqual(sum(bank_account_lines.mapped("credit")), 73.33)
        self.assertEqual(sum(bank_account_lines.mapped("amount_currency")), 125.0)

    def test_revaluation_bank_account_same_currency(self):
        self.delete_journal_data()
        usd_currency = self.env.ref("base.USD")
        eur_currency = self.env.ref("base.EUR")
        rates = {
            (self.today - timedelta(days=90)): 0.75,
            (self.today - timedelta(days=80)): 1.00,
            (self.today - timedelta(days=70)): 1.25,
        }
        self.create_rates(rates, eur_currency)
        values = {
            "reversable_revaluations": False,
            "currency_id": usd_currency.id,
        }
        self.company.write(values)

        usd_bank = self.env["account.journal"].create(
            {
                "name": "USD Bank",
                "type": "bank",
                "code": "USDBK",
                "currency_id": usd_currency.id,
            }
        )
        usd_bank.default_debit_account_id.currency_revaluation = True
        bank_account = usd_bank.default_debit_account_id

        liability_account = self.env["account.account"].create(
            {
                "name": "Liability",
                "code": "L",
                "user_type_id": self.env.ref(
                    "account.data_account_type_current_liabilities"
                ).id,
                "company_id": self.company.id,
            }
        )

        bank_stmt = self.env["account.bank.statement"].create(
            {
                "journal_id": usd_bank.id,
                "date": self.today - timedelta(days=60),
                "name": "Statement",
            }
        )

        bank_stmt_line_1 = self.env["account.bank.statement.line"].create(
            {
                "name": "Incoming 100 USD",
                "statement_id": bank_stmt.id,
                "amount": 100.0,
                "date": "2020-11-10",
            }
        )
        bank_stmt_line_1.process_reconciliation(
            new_aml_dicts=[
                {
                    "credit": 100.0,
                    "debit": 0.0,
                    "name": "Incoming 100 USD",
                    "account_id": liability_account.id,
                }
            ]
        )

        bank_stmt_line_2 = self.env["account.bank.statement.line"].create(
            {
                "name": "Outgoing 25 USD",
                "statement_id": bank_stmt.id,
                "amount": -25.0,
                "date": self.today - timedelta(days=79),
            }
        )
        invoice = self.create_invoice(
            self.today - timedelta(days=79), usd_currency, 1.0, 25.0
        )
        invoice.post()
        invoice_move_line = next(
            move_line
            for move_line in invoice.line_ids
            if move_line.account_id == self.receivable_acc
        )
        bank_stmt_line_2.process_reconciliation(
            counterpart_aml_dicts=[
                {
                    "move_line": invoice_move_line,
                    "credit": 0,
                    "debit": 25,
                    "name": invoice_move_line.name,
                }
            ]
        )

        bank_stmt_line_3 = self.env["account.bank.statement.line"].create(
            {
                "name": "Incoming 50 USD",
                "statement_id": bank_stmt.id,
                "amount": 50.0,
                "date": self.today - timedelta(days=69),
                "amount_currency": 50.0,
            }
        )
        bank_stmt_line_3.process_reconciliation(
            new_aml_dicts=[
                {
                    "credit": 50.0,
                    "debit": 0.0,
                    "name": "Incoming 50 USD",
                    "account_id": liability_account.id,
                }
            ]
        )

        bank_stmt_line_4 = self.env["account.bank.statement.line"].create(
            {
                "name": "Incoming 50 EUR",
                "statement_id": bank_stmt.id,
                "amount": 62.5,
                "date": self.today - timedelta(days=69),
                "amount_currency": 50.0,
                "currency_id": eur_currency.id,
            }
        )
        bank_stmt_line_4.process_reconciliation(
            new_aml_dicts=[
                {
                    "credit": 50,
                    "debit": 0.0,
                    "name": "Incoming 50 EUR",
                    "account_id": liability_account.id,
                }
            ]
        )

        bank_account_lines = self.env["account.move.line"].search(
            [("account_id", "=", bank_account.id)]
        )
        self.assertEqual(len(bank_account_lines), 4)
        self.assertEqual(sum(bank_account_lines.mapped("debit")), 212.5)
        self.assertEqual(sum(bank_account_lines.mapped("credit")), 25.0)

        with self.assertRaises(exceptions.Warning):
            self.wizard_execute(self.today - timedelta(days=60))

        self.assertEqual(
            bank_account_lines,
            self.env["account.move.line"].search(
                [("account_id", "=", bank_account.id)]
            ),
        )

    def test_revaluation_reverse(self):
        self.delete_journal_data()
        usd_currency = self.env.ref("base.USD")
        eur_currency = self.env.ref("base.EUR")
        rates = {
            (self.today - timedelta(days=90)): 0.75,
            (self.today - timedelta(days=80)): 1.00,
            (self.today - timedelta(days=70)): 1.25,
        }
        self.create_rates(rates, eur_currency)
        values = {
            "reversable_revaluations": True,
            "currency_id": usd_currency.id,
        }
        self.company.write(values)

        eur_bank = self.env["account.journal"].create(
            {
                "name": "EUR Bank",
                "type": "bank",
                "code": "EURBK",
                "currency_id": eur_currency.id,
            }
        )
        eur_bank.default_debit_account_id.currency_revaluation = True
        bank_account = eur_bank.default_debit_account_id

        liability_account = self.env["account.account"].create(
            {
                "name": "Liability",
                "code": "L",
                "user_type_id": self.env.ref(
                    "account.data_account_type_current_liabilities"
                ).id,
                "company_id": self.company.id,
            }
        )

        bank_stmt = self.env["account.bank.statement"].create(
            {
                "journal_id": eur_bank.id,
                "date": self.today - timedelta(days=60),
                "name": "Statement",
            }
        )

        bank_stmt_line_1 = self.env["account.bank.statement.line"].create(
            {
                "name": "Incoming 100 EUR",
                "statement_id": bank_stmt.id,
                "amount": 100.0,
                "date": self.today - timedelta(days=90),
            }
        )
        bank_stmt_line_1.process_reconciliation(
            new_aml_dicts=[
                {
                    "credit": 100.0,
                    "debit": 0.0,
                    "name": "Incoming 100 EUR",
                    "account_id": liability_account.id,
                }
            ]
        )

        bank_stmt_line_2 = self.env["account.bank.statement.line"].create(
            {
                "name": "Outgoing 25 EUR",
                "statement_id": bank_stmt.id,
                "amount": -25.0,
                "date": self.today - timedelta(days=79),
            }
        )
        invoice = self.create_invoice(
            self.today - timedelta(days=79), eur_currency, 1.0, 25.0
        )
        invoice.post()
        invoice_move_line = next(
            move_line
            for move_line in invoice.line_ids
            if move_line.account_id == self.receivable_acc
        )
        bank_stmt_line_2.process_reconciliation(
            counterpart_aml_dicts=[
                {
                    "move_line": invoice_move_line,
                    "credit": 0,
                    "debit": 25,
                    "name": invoice_move_line.name,
                }
            ]
        )

        bank_stmt_line_3 = self.env["account.bank.statement.line"].create(
            {
                "name": "Incoming 50 EUR",
                "statement_id": bank_stmt.id,
                "amount": 50.0,
                "date": self.today - timedelta(days=69),
            }
        )
        bank_stmt_line_3.process_reconciliation(
            new_aml_dicts=[
                {
                    "credit": 50.0,
                    "debit": 0.0,
                    "name": "Incoming 50 EUR",
                    "account_id": liability_account.id,
                }
            ]
        )

        bank_account_lines = self.env["account.move.line"].search(
            [("account_id", "=", bank_account.id)]
        )
        self.assertEqual(len(bank_account_lines), 3)
        self.assertEqual(sum(bank_account_lines.mapped("debit")), 173.33)
        self.assertEqual(sum(bank_account_lines.mapped("credit")), 25.0)
        self.assertEqual(sum(bank_account_lines.mapped("amount_currency")), 125.0)

        result = self.wizard_execute(self.today - timedelta(days=60))
        self.assertEqual(result.get("name"), "Created revaluation lines")

        new_bank_account_lines = self.env["account.move.line"].search(
            [("account_id", "=", bank_account.id)]
        )
        revaluation_lines = new_bank_account_lines - bank_account_lines
        self.assertEqual(len(revaluation_lines), 2)
        self.assertEqual(sum(revaluation_lines.mapped("debit")), 48.33)
        self.assertEqual(sum(revaluation_lines.mapped("credit")), 48.33)
        self.assertEqual(sum(revaluation_lines.mapped("amount_currency")), 0.0)

    def create_rates(self, rates_by_date, currency, purge=False):
        if purge:
            old_rates = self.env["res.currency.rate"].search([])
        else:
            old_rates = currency.rate_ids
        old_rates.unlink()
        for date, rate in rates_by_date.items():
            self.env["res.currency.rate"].create(
                {
                    "currency_id": currency.id,
                    "company_id": self.company.id,
                    "name": date,
                    "rate": rate,
                }
            )

    def create_invoice(self, date, currency, quantity, price):
        revenue_acc = self.env.ref("account_multicurrency_revaluation.demo_acc_revenue")
        payment_term = self.env.ref("account.account_payment_term_end_following_month")
        sales_journal = self.env.ref("account_multicurrency_revaluation.sales_journal")
        invoice_line_data = {
            "product_id": self.env.ref("product.product_product_5").id,
            "quantity": quantity,
            "account_id": revenue_acc.id,
            "name": "product test 5",
            "price_unit": price,
            "currency_id": currency.id,
        }
        invoice = self.env["account.move"].create(
            {
                "type": "out_invoice",
                "invoice_date": date,
                "currency_id": currency.id,
                "journal_id": sales_journal.id,
                "company_id": self.company.id,
                "partner_id": self.partner.id,
                "invoice_line_ids": [(0, 0, invoice_line_data)],
                "invoice_payment_term_id": payment_term.id,
            }
        )
        return invoice

    def wizard_execute(self, date):
        wiz = self.env["wizard.currency.revaluation"].create(
            {
                "revaluation_date": date,
                "journal_id": self.reval_journal.id,
                "label": "[%(account)s] [%(currency)s] wiz_test",
            }
        )
        return wiz.revaluate_currency()

    def delete_journal_data(self):
        """Delete journal data
        delete all journal-related data, so a new currency can be set.
        """

        company = self.company
        invoices = self.env["account.move"].search([("company_id", "=", company.id)])
        invoices.button_draft()

        models_to_clear = [
            "account.move.line",
            "account.move",
            "account.bank.statement",
        ]
        for model in models_to_clear:
            records = (
                self.env[model]
                .with_context(force_delete=True)
                .search([("company_id", "=", company.id)])
            )
            if model == "account.move.line":
                records.remove_move_reconcile()
            records.unlink()