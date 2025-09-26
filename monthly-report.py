#!/usr/local/bin/python3.7

import os
import yaml
import sys
import traceback
import datetime
import requests
import re
import bs4
import ssl
import smtplib
import logging

from email.message import EmailMessage
from email.headerregistry import Address
from email.utils import make_msgid

def _configure_logging():
    level_name = os.environ.get("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s: %(message)s",
    )

def parse_bool(val, default=False):
    if val is None:
        return default
    if isinstance(val, bool):
        return val
    return str(val).strip().lower() in ("1", "true", "yes", "on")

def env_or(cfg, env_name, cfg_path=None, default=None):
    v = os.environ.get(env_name)
    if v is not None:
        return v
    if cfg is not None and cfg_path:
        cur = cfg
        try:
            for k in cfg_path:
                cur = cur[k]
            return cur
        except Exception:
            return default
    return default

def load_configuration():
    cfg = None
    try:
        if os.path.exists("config.yaml"):
            with open("config.yaml", "r") as f:
                cfg = yaml.safe_load(f) or {}
            logging.debug("Loaded config.yaml")
    except Exception:
        logging.exception("Failed to parse config.yaml; continuing with environment variables only")

    config = {}
    config['firefly-url'] = env_or(cfg, "FIREFLY_URL", ['firefly-url'], None)
    config['accesstoken'] = env_or(cfg, "ACCESSTOKEN", ['accesstoken'], None)
    config['currency'] = env_or(cfg, "CURRENCY", ['currency'], None)
    config['currencySymbol'] = env_or(cfg, "CURRENCYSYMBOL", ['currencySymbol'], None)

    email_from = env_or(cfg, "EMAIL_FROM", ['email','from'], None)
    email_to = env_or(cfg, "EMAIL_TO", ['email','to'], None)

    if isinstance(email_to, str):
        to_list = [e.strip() for e in email_to.split(",") if e.strip()]
    elif isinstance(email_to, list):
        to_list = email_to
    elif isinstance(email_to, tuple):
        to_list = list(email_to)
    else:
        to_list = []

    config['email'] = {'from': email_from, 'to': to_list}

    smtp_server = env_or(cfg, "SMTP_SERVER", ['smtp','server'], None)
    smtp_port = env_or(cfg, "SMTP_PORT", ['smtp','port'], None)
    smtp_starttls = env_or(cfg, "SMTP_STARTTLS", ['smtp','starttls'], None)
    smtp_auth = env_or(cfg, "SMTP_AUTHENTICATION", ['smtp','authentication'], None)
    smtp_user = env_or(cfg, "SMTP_USER", ['smtp','user'], None)
    smtp_password = env_or(cfg, "SMTP_PASSWORD", ['smtp','password'], None)

    try:
        smtp_port = int(smtp_port) if smtp_port is not None else None
    except Exception:
        smtp_port = None

    smtp_starttls = parse_bool(smtp_starttls, default=True)
    smtp_auth = parse_bool(smtp_auth, default=True)

    config['smtp'] = {
        'server': smtp_server,
        'port': smtp_port,
        'starttls': smtp_starttls,
        'authentication': smtp_auth,
        'user': smtp_user,
        'password': smtp_password,
    }

    missing = []
    if not config.get('firefly-url'):
        missing.append("FIREFLY_URL or firefly-url in config.yaml")
    if not config.get('accesstoken'):
        missing.append("ACCESSTOKEN or accesstoken in config.yaml")
    if not config['smtp'].get('server'):
        missing.append("SMTP_SERVER or smtp.server in config.yaml")
    if not config['smtp'].get('port'):
        missing.append("SMTP_PORT or smtp.port in config.yaml")
    if not config['email'].get('from'):
        missing.append("EMAIL_FROM or email.from in config.yaml")
    if not config['email'].get('to'):
        missing.append("EMAIL_TO or email.to in config.yaml")

    if missing:
        logging.error("Missing configuration values:")
        for m in missing:
            logging.error("  - %s", m)
        sys.exit(1)

    logging.info("Configuration loaded successfully")
    return config

def main():
    _configure_logging()
    logging.info("Starting monthly-report script")
    try:
        config = load_configuration()

        today = datetime.date.today()
        endDate = today.replace(day=1) - datetime.timedelta(days=1)
        startDate = endDate.replace(day=1)
        monthName = startDate.strftime("%B")
        logging.debug("Date range: %s to %s", startDate.isoformat(), endDate.isoformat())

        HEADERS = {'Authorization': 'Bearer {}'.format(config['accesstoken'])}
        with requests.Session() as s:
            s.headers.update(HEADERS)

            try:
                about = s.get(config['firefly-url'] + '/api/v1/about')
                logging.info("Server about: %s", about.text)
            except Exception:
                logging.exception("Failed to fetch server about endpoint")

            url = config['firefly-url'] + '/api/v1/categories'
            logging.info("Fetching categories from %s", url)
            categories = s.get(url).json()

            totals = []
            for category in categories['data']:
                url = config['firefly-url'] + '/api/v1/categories/' + category['id'] + '?start=' + \
                    startDate.strftime('%Y-%m-%d') + '&end=' + \
                    endDate.strftime('%Y-%m-%d')
                r = s.get(url).json()
                categoryName = r['data']['attributes']['name']
                try:
                    categorySpent = r['data']['attributes']['spent'][0]['sum']
                except (KeyError, IndexError):
                    categorySpent = 0
                try:
                    categoryEarned = r['data']['attributes']['earned'][0]['sum']
                except (KeyError, IndexError):
                    categoryEarned = 0
                categoryTotal = float(categoryEarned) + float(categorySpent)
                totals.append({'name': categoryName, 'spent': categorySpent,
                              'earned': categoryEarned, 'total': categoryTotal})
            logging.info("Fetched %d categories", len(totals))

            budgetsUrl = config['firefly-url'] + '/api/v1/budgets'
            budgetsCategories = s.get(budgetsUrl).json()
            budgets = []
            for b in budgetsCategories['data']:
                budgets.append({
                    'id': b['id'],
                    'name': b['attributes']['name'],
                    'budgeted': b['attributes']['auto_budget_amount'],
                })

            for budget in budgets:
                bid = budget.get('id')
                logging.info("Processing budget '%s' (id %s)", budget['name'], bid)
                budgetsSpentUrl = f"{config['firefly-url']}/api/v1/budgets/{bid}/limits?start={startDate.strftime('%Y-%m-%d')}&end={endDate.strftime('%Y-%m-%d')}"
                budgetsSpentCategories = s.get(budgetsSpentUrl).json()

                if budgetsSpentCategories.get('message') == 'Resource not found':
                    logging.warning("Budget id %s not found, skipping", bid)
                    budget['spent'] = 0.0
                    continue

                try:
                    data = budgetsSpentCategories.get('data', []) or []
                    if not data:
                        logging.warning("No data returned for budget id %s; treating spent as 0", bid)
                        spentInCurrency = 0.0
                    else:
                        attrs = data[0].get('attributes', {}) or {}
                        spent_list = attrs.get('spent') or []
                        if not spent_list:
                            logging.debug("No 'spent' entries for budget id %s; treating spent as 0", bid)
                            spentInCurrency = 0.0
                        else:
                            preferred_currency = config.get('currency', 'EUR')
                            match = next((x for x in spent_list if x.get('currency_code') == preferred_currency), None)
                            if match is None:
                                match = spent_list[0]
                            spentInCurrency = float(match.get('sum') or 0.0)
                except Exception:
                    logging.exception("Failed to parse spent for budget id %s", bid)
                    spentInCurrency = 0.0

                try:
                    included_name = budgetsSpentCategories.get('included', [{}])[0].get('attributes', {}).get('name', budget['name'])
                except Exception:
                    included_name = budget.get('name', 'unknown')

                logging.info("Budget '%s' spent: %s", included_name, spentInCurrency)
                budget['spent'] = round(abs(float(spentInCurrency)), 2)

            monthSummary = s.get(config['firefly-url'] + '/api/v1/summary/basic' + '?start=' +
                                 startDate.strftime('%Y-%m-%d') + '&end=' + endDate.strftime('%Y-%m-%d')).json()
            yearToDateSummary = s.get(config['firefly-url'] + '/api/v1/summary/basic' + '?start=' +
                                      startDate.strftime('%Y') + '-01-01' + '&end=' + endDate.strftime('%Y-%m-%d')).json()
            currency = config.get('currency', None)
            currencySymbol = config.get('currencySymbol', None)
            if currency:
                currencyName = currency
            else:
                for key in monthSummary:
                    if re.match(r'spent-in-.*', key):
                        currencyName = key.replace("spent-in-", "")
            try:
                spentThisMonth = abs(float(
                    monthSummary['spent-in-'+currencyName]['monetary_value']))
                earnedThisMonth = float(
                    monthSummary['earned-in-'+currencyName]['monetary_value'])
                netChangeThisMonth = float(
                    monthSummary['balance-in-'+currencyName]['monetary_value'])
                spentThisYear = float(
                    yearToDateSummary['spent-in-'+currencyName]['monetary_value'])
                earnedThisYear = float(
                    yearToDateSummary['earned-in-'+currencyName]['monetary_value'])
                netChangeThisYear = float(
                    yearToDateSummary['balance-in-'+currencyName]['monetary_value'])
                netWorth = float(
                    yearToDateSummary['net-worth-in-'+currencyName]['monetary_value'])
            except Exception:
                logging.exception("Failed to parse summaries, defaulting numeric fields to 0")
                spentThisMonth = earnedThisMonth = netChangeThisMonth = 0.0
                spentThisYear = earnedThisYear = netChangeThisYear = netWorth = 0.0

            savedThisMonth = round(earnedThisMonth - spentThisMonth)
            savedPercentage = round((savedThisMonth / earnedThisMonth) * 100) if earnedThisMonth else 0
            spendPercentage = 100 - savedPercentage

            categoriesTableBody = '<table><tr><th>Category</th><th style="text-align: right;">Total</th></tr>'
            for category in totals:
                categoriesTableBody += '<tr><td style="padding-right: 1em;">' + \
                    category['name']+'</td><td style="text-align: right;">' + \
                    str(round(float(category['total']))).replace(
                        "-", "−")+'</td></tr>'
            categoriesTableBody += '</table>'

            budgetsTableBody = '<table><tr><th>Category</th><th style="text-align: right;">Total</th></tr>'
            totalBudgetsAmount = 0
            for category in budgets:
                totalBudgetsAmount += round(float(category.get('budgeted', 0)))
                budgetsTableBody += '<tr><td style="padding-right: 1em;">' + \
                    category['name']+'</td><td style="text-align: right;">' + \
                    str(round(float(category.get('budgeted', 0))))+'</td></tr>'
            budgetsTableBody += '</table>'

            generalTableBody = '<table>'
            generalTableBody += '<tr><td>Spent this month:</td><td style="text-align: right;">' + \
                str(round(spentThisMonth)).replace("-", "−") + '</td></tr>'
            generalTableBody += '<tr><td>Earned this month:</td><td style="text-align: right;">' + \
                str(round(earnedThisMonth)).replace("-", "−") + '</td></tr>'
            generalTableBody += '<tr style="border-bottom: 1px solid black"><td>Net change this month:</td><td style="text-align: right;">' + \
                str(round(netChangeThisMonth)).replace("-", "−") + '</td></tr>'
            generalTableBody += '<tr><td>Spent so far this year:</td><td style="text-align: right;">' + \
                str(round(spentThisYear)).replace("-", "−") + '</td></tr>'
            generalTableBody += '<tr><td>Earned so far this year:</td><td style="text-align: right;">' + \
                str(round(earnedThisYear)).replace("-", "−") + '</td></tr>'
            generalTableBody += '<tr style="border-bottom: 1px solid black"><td style="padding-right: 1em;">Net change so far this year:</td><td style="text-align: right;">' + \
                str(round(netChangeThisYear)).replace("-", "−") + '</td></tr>'
            generalTableBody += '<tr><td>Current net worth:</td><td style="text-align: right;">' + \
                str(round(netWorth)).replace("-", "−") + '</td></tr>'
            generalTableBody += '</table>'

            def getCategories(budget):
                pageNumber = 1
                categoriesAmount = []
                index = 1
                for i, item in enumerate(budgets):
                    if item["name"] == budget:
                        index = i + 1
                        break
                while True:
                    budgetsCategoryUrl = config['firefly-url'] + f'/api/v1/budgets/{index}/limits/{index}/transactions?limit=50&page={pageNumber}' + '&start=' + startDate.strftime('%Y-%m-%d') + '&end=' + endDate.strftime('%Y-%m-%d')
                    transactionCategories = s.get(budgetsCategoryUrl).json()
                    for category in transactionCategories['data']:
                        name = category['attributes']['transactions'][0]['category_name']
                        amount = category['attributes']['transactions'][0]['amount']
                        categoriesAmount.append({'name': name, 'spent': round(float(amount), 2)})
                    if pageNumber < transactionCategories['meta']['pagination']['total_pages']:
                        pageNumber += 1
                    else:
                        break
                sums = {}
                for item in categoriesAmount:
                    name = item['name']
                    spent = float(item['spent'])
                    if name not in sums:
                        sums[name] = 0
                    sums[name] += spent
                sorted_sums = dict(sorted(sums.items(), key=lambda x: x[1], reverse=True))
                html_result = '<p style="margin-top: 10px">'
                html_result += 'Categories: <br />'
                for category, total_spent in sorted_sums.items():
                    html_result += f"- {category}: {currencySymbol}{total_spent:.2f} <br />"
                html_result += '</p>'
                return html_result

            goodBudgeting = '''   
            <div
                        class="loading-bar-2"
                        style="
                        border: 2px solid #2ca58d;
                        border-radius: 20px;
                        padding: 10px;
                        margin-bottom: 20px;
                        "
                    >
                        <div
                        class="loading-bar-name"
                        style="display: flex; justify-content: center; font-weight: bold"
                        >
                        <p style="margin-top: 10px">{budgetName}</p>
                        </div>
                        <div class="loading-bar-progress-2">
                        <div
                            class="loading-bar-progress"
                            style="border: 2px solid #2ca58d; border-radius: 10px"
                        >
                            <div
                            class="loading-bar-fill"
                            style="
                                background-color: #2ca58d;
                                height: 20px;
                                width: {percentage}%;
                                border-radius: 10px;
                                position: relative;
                            "
                            >
                            <span
                                class="loading-bar-percentage"
                                style="
                                position: absolute;
                                top: 50%;
                                left: 50%;
                                transform: translate(-50%, -50%);
                                color: #ffffff;
                                "
                                > {percentage}%</span
                            >
                            </div>
                        </div>
                        </div>
                        <p style="margin-top: 10px">Budget size: {currencySymbol}{budgetPlanned}</p>
                        <p style="margin-top: 10px">Paid: {currencySymbol}{spent}</p>
                        <p style="margin-top: 10px">Saved: {currencySymbol}{saved}</p>
                        <div class="budget-message respected">
                        <p>Your Budget is being Respectfully Managed! 🌟</p>
                        </div>
                    </div>
            '''
            badBudgeting = '''
            <div
                        class="loading-bar"
                        style="
                        border: 2px solid #89023e;
                        border-radius: 20px;
                        padding: 10px;
                        margin-bottom: 20px;
                        "
                    >
                        <div
                        class="loading-bar-name"
                        style="display: flex; justify-content: center; font-weight: bold"
                        >
                        <p style="margin-top: 10px">{budgetName}</p>
                        </div>
                        <div
                        class="loading-bar-progress"
                        style="border: 2px solid #89023e; border-radius: 10px"
                        >
                        <div
                            class="loading-bar-fill"
                            style="
                            background-color: #89023e;
                            height: 20px;
                            width: 100%;
                            border-radius: 20px;
                            position: relative;
                            "
                        >
                            <span
                            class="loading-bar-percentage"
                            style="
                                position: absolute;
                                top: 50%;
                                left: 50%;
                                transform: translate(-50%, -50%);
                                color: #ffffff;
                            "
                            > {percentage}% 💀</span
                            >
                        </div>
                        </div>
                        <p style="margin-top: 10px">Budget size: {currencySymbol}{budgetPlanned}</p>
                        <p style="margin-top: 10px">Paid: {currencySymbol}{spent}</p>
                        <div class="budget-message overspent">
                        <p>Oops! Some Overspending Detected! 😅</p>
                        <p style="margin-top: 10px">
                            Explore your expenses in these categories 🕵️‍♂️
                        </p>
                        </div>
                        {overspentCategories}
                    </div>
            '''
            budgetsMonthlyList = ''
            for budget in budgets:
                budgeted = float(budget.get('budgeted', 0))
                spent_val = float(budget.get('spent', 0))
                if budgeted > spent_val:
                    pct = round((round(spent_val) / round(budgeted)) * 100) if budgeted else 0
                    budgetsMonthlyList += goodBudgeting.format(budgetName=budget['name'], currencySymbol=currencySymbol, budgetPlanned=round(float(budgeted)), spent=round(float(spent_val)), saved=round(float(budgeted)) - round(float(spent_val)), percentage=pct)
                else:
                    pct = round((round(spent_val) / round(budgeted)) * 100) if budgeted else 100
                    budgetsMonthlyList += badBudgeting.format(budgetName=budget['name'], currencySymbol=currencySymbol, budgetPlanned=round(float(budgeted)), overspentCategories=getCategories(budget['name']), spent=round(float(spent_val)), percentage=pct)

            msg = EmailMessage()
            msg['Subject'] = "Firefly III: Monthly report"
            msg['From'] = "monthly-report <" + config['email']['from'] + ">"
            msg['To'] = ", ".join(config['email']['to'])
            htmlBody = """
            <html lang="en">
            <head>
                <meta charset="UTF-8" />
                <meta name="viewport" content="width=device-width, initial-scale=1.0" />
                <title>FireFly III Monthly Report</title>
            </head>
            <body
                style="
                margin: 0;
                font-family: Arial, sans-serif;
                line-height: 1.6;
                background-color: #f5f5f5;
                "
            >
                <table
                class="container"
                cellpadding="0"
                cellspacing="0"
                border="0"
                align="center"
                style="
                    width: 100%;
                    max-width: 600px;
                    margin: 0 auto;
                    background-color: #ffffff;
                "
                >
                <div
                    class="navbar"
                    style="
                    background-color: #ffbf46;
                    color: #ffffff;
                    text-align: center;
                    padding: 10px 0;
                    "
                >
                    <h1>FireFly III</h1>
                </div>
                <tr>
                    <td class="header" style="padding: 40px 20px; text-align: center">
                    <h1 style="margin: 0; color: #333333">
                        📊 Are you rocking your budgets? 🚀
                    </h1>
                    </td>
                </tr>
                <tr>
                    <td class="body-content" style="padding: 40px 20px">
                    <p style="margin-bottom: 20px">Hey there, Budget Boss! 🎉</p>
                    <p style="margin-bottom: 20px">
                        Here comes your monthly review for {monthName} {year} - a treasure trove of insights into
                        your spending habits and financial triumphs! 💰✨
                    </p>
                    {budgetsMonthlylList}
                    <div
                        class="loading-bar-2"
                        style="
                        border: 2px solid #735cdd;
                        border-radius: 20px;
                        padding: 10px;
                        margin-bottom: 20px;
                        "
                    >
                        <div
                        class="loading-bar-name"
                        style="display: flex; justify-content: center; font-weight: bold"
                        >
                        <p style="margin-top: 10px">{monthName} review</p>
                        </div>
                        <div class="loading-bar-progress-2">
                        <div
                            class="loading-bar-progress"
                            style="border: 2px solid #735cdd; border-radius: 20px"
                        >
                            <div
                            class="loading-bar-fill"
                            style="
                                background-color: #735cdd;
                                height: 20px;
                                width: {spendPercentage}%;
                                border-radius: 10px;
                                position: relative;
                            "
                            >
                            <span
                                class="loading-bar-percentage"
                                style="
                                position: absolute;
                                top: 50%;
                                left: 50%;
                                transform: translate(-50%, -50%);
                                color: #ffffff;
                                "
                                > {spendPercentage}%</span
                            >
                            </div>
                        </div>
                        </div>
                        <p style="margin-top: 10px">Earned: {currencySymbol}{earnedThisMonth}</p>
                        <p style="margin-top: 10px">Total budgeted: {currencySymbol}{totalBudgetsAmount}</p>
                        <p style="margin-top: 10px">Paid: {currencySymbol}{spentThisMonth}</p>
                        <p style="margin-top: 10px">Saved: {currencySymbol}{savedThisMonth} or {savedPercentage}%</p>
                        <div class="budget-message general-info">
                        <p>🌈 "Financial freedom is the new rich." - Unknown 🌟</p>
                        </div>
                    </div>
                    <p style="margin-bottom: 20px">
                        Cheers, <br />Your Budgeting Buddy 🌟
                    </p>
                    </td>
                </tr>
                </table>
            </body>
            </html>
            """.format(monthName=monthName, year=startDate.strftime("%Y"), currencySymbol=currencySymbol, totalBudgetsAmount=totalBudgetsAmount, budgetsMonthlylList=budgetsMonthlyList, spendPercentage=spendPercentage, savedPercentage=savedPercentage, savedThisMonth=savedThisMonth, spentThisMonth=round(spentThisMonth), earnedThisMonth=round(earnedThisMonth))
            msg.set_content(bs4.BeautifulSoup(
                htmlBody, "html.parser").get_text())
            msg.add_alternative(htmlBody, subtype='html')

            context = ssl.create_default_context()

            with smtplib.SMTP(host=config['smtp']['server'], port=config['smtp']['port']) as smtp_conn:
                if config['smtp']['starttls']:
                    smtp_conn.ehlo()
                    try:
                        smtp_conn.starttls(context=context)
                    except Exception:
                        logging.exception("Could not connect to SMTP server with STARTTLS")
                        sys.exit(2)
                if config['smtp']['authentication']:
                    try:
                        smtp_conn.login(user=config['smtp']['user'],
                                       password=config['smtp']['password'])
                    except Exception:
                        logging.exception("Could not authenticate with SMTP server.")
                        sys.exit(3)
                try:
                    smtp_conn.send_message(msg)
                    logging.info("Monthly report sent to %s", ", ".join(config['email']['to']))
                except Exception:
                    logging.exception("Failed to send monthly report")

    except Exception:
        logging.exception("Unhandled exception in main")
        sys.exit(99)

if __name__ == "__main__":
    main()
