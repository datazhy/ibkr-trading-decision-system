"""Flex 报表测试夹具。

**层级结构照抄真实生产报表，数据全部虚构。** 真实报表本身留在服务器的
secrets/ 目录（权限 600），绝不进入测试目录或 Git。

之所以要按真实层级构造，是因为线上暴露的问题全部出在结构假设上：

  · 外层是 FlexQueryResponse，不是 FlexStatements
  · Statement of Funds 的容器叫 `StmtFunds`，不是 `StatementOfFunds`
  · 同一笔成交在 Trade 与 StatementOfFundsLine 各出现一次
  · Trade.tradeID 与 Trade.transactionID **不相同**

用扁平的假 XML 测不出这些，只能照着真实骨架来。
"""

TEST_ACCOUNT = "TEST_ACCOUNT"


def production_shaped(*, namespace: bool = False, wrapper: str = "full") -> str:
    """生成与真实报表同构的虚构报表。

    wrapper:
      full      -> FlexQueryResponse → FlexStatements → FlexStatement
      statements-> FlexStatements → FlexStatement
      bare      -> 裸 FlexStatement
    """
    ns = ' xmlns="http://x.example/flex"' if namespace else ""

    body = f"""<FlexStatement accountId="{TEST_ACCOUNT}" fromDate="20260401"
   toDate="20260630" period="LastNDays" whenGenerated="20260701;120000">
  <AccountInformation accountId="{TEST_ACCOUNT}" currency="USD"
    accountType="INDIVIDUAL" masterName="TEST NAME"/>
  <CashTransactions>
    <CashTransaction accountId="{TEST_ACCOUNT}" type="Deposits/Withdrawals"
      amount="10000" currency="USD" dateTime="20260402;120000"
      settleDate="20260402" description="TEST DEPOSIT" transactionID="C001"
      levelOfDetail="DETAIL" fxRateToBase="1"/>
    <CashTransaction accountId="{TEST_ACCOUNT}" type="Broker Interest Received"
      amount="12.34" currency="USD" dateTime="20260430;120000"
      description="TEST INTEREST" transactionID="C002"
      levelOfDetail="DETAIL" fxRateToBase="1"/>
  </CashTransactions>
  <StmtFunds>
    <StatementOfFundsLine accountId="{TEST_ACCOUNT}" activityCode=""
      activityDescription="Opening Balance" amount="0" balance="1000"
      currency="USD" date="20260401" reportDate="20260401"
      levelOfDetail="Currency" fxRateToBase="1"/>
    <StatementOfFundsLine accountId="{TEST_ACCOUNT}" activityCode=""
      activityDescription="Closing Balance" amount="0" balance="8999"
      currency="USD" date="20260630" reportDate="20260630"
      levelOfDetail="Currency" fxRateToBase="1"/>
    <StatementOfFundsLine accountId="{TEST_ACCOUNT}" activityCode=""
      activityDescription="Opening Balance" amount="0" balance="500"
      currency="HKD" date="20260401" reportDate="20260401"
      levelOfDetail="Currency" fxRateToBase="0.128"/>
    <StatementOfFundsLine accountId="{TEST_ACCOUNT}" activityCode=""
      activityDescription="Closing Balance" amount="0" balance="500"
      currency="HKD" date="20260630" reportDate="20260630"
      levelOfDetail="Currency" fxRateToBase="0.128"/>
    <StatementOfFundsLine accountId="{TEST_ACCOUNT}" activityCode="DEP"
      activityDescription="TEST DEPOSIT" amount="10000" balance="10000"
      currency="USD" date="20260402" reportDate="20260402"
      transactionID="C001" levelOfDetail="Currency" fxRateToBase="1"/>
    <StatementOfFundsLine accountId="{TEST_ACCOUNT}" activityCode="BUY"
      activityDescription="TEST BUY" amount="-2000" balance="8000"
      currency="USD" date="20260410" reportDate="20260410" symbol="TEST1"
      conid="111" tradeID="T900" transactionID="X001"
      levelOfDetail="Currency" fxRateToBase="1"/>
    <StatementOfFundsLine accountId="{TEST_ACCOUNT}" activityCode=""
      activityDescription="Adjustment" amount="-25" balance="8974"
      currency="USD" date="20260415" reportDate="20260415"
      transactionID="ADJ1" levelOfDetail="Currency" fxRateToBase="1"/>
  </StmtFunds>
  <Trades>
    <Trade accountId="{TEST_ACCOUNT}" tradeID="T900" transactionID="X001"
      symbol="TEST1" conid="111" buySell="BUY" netCash="-2000"
      ibCommission="-1.00" ibCommissionCurrency="USD" currency="USD"
      dateTime="20260410;100000" tradeDate="20260410" quantity="20"
      proceeds="-2000" cost="2000" levelOfDetail="EXECUTION"
      transactionType="ExchTrade" fxRateToBase="1"/>
    <Trade accountId="{TEST_ACCOUNT}" tradeID="T901" transactionID="X002"
      symbol="TEST2" conid="222" buySell="SELL" netCash="3000"
      ibCommission="-1.20" ibCommissionCurrency="USD" currency="USD"
      dateTime="20260520;100000" tradeDate="20260520" quantity="-30"
      proceeds="3001" cost="2500" fifoPnlRealized="500.50"
      levelOfDetail="EXECUTION" transactionType="ExchTrade" fxRateToBase="1"/>
  </Trades>
  <ConversionRates>
    <ConversionRate reportDate="20260401" fromCurrency="USD"
      toCurrency="USD" rate="1"/>
    <ConversionRate reportDate="20260401" fromCurrency="HKD"
      toCurrency="USD" rate="0.128"/>
    <ConversionRate reportDate="20260630" fromCurrency="HKD"
      toCurrency="USD" rate="0.129"/>
  </ConversionRates>
  <ChangeInNAV accountId="{TEST_ACCOUNT}" currency="USD"
    fromDate="20260729" toDate="20260729" startingValue="40000"
    mtm="-100" commissions="-1" depositsWithdrawals="0"
    internalCashTransfers="0" assetTransfers="0" endingValue="39899"
    twr="-0.2525"/>
  <ChangeInNAV accountId="{TEST_ACCOUNT}" currency="USD"
    fromDate="20260730" toDate="20260730" startingValue="39899"
    mtm="8648.562" commissions="-2.441458" depositsWithdrawals="0"
    internalCashTransfers="0" assetTransfers="0" endingValue="48545.120542"
    twr="21.670"/>
  <Transfers>
    <Transfer accountId="{TEST_ACCOUNT}" reportDate="20260615" date="20260615"
      settleDate="20260616" type="ACATS" direction="IN" deliveringBroker="0000"
      currency="USD" assetCategory="STK" symbol="TESTX" description="TEST STOCK"
      conid="333" quantity="10" transferPrice="0" costBasis="900"
      positionAmount="1000" positionAmountInBase="1000" pnlAmount="0"
      pnlAmountInBase="0" cashTransfer="0" transactionID="TR001"
      transferAccount="SENSITIVE-OTHER-ACCOUNT" transferAccountName="TEST PERSON"
      levelOfDetail="TRANSFER"/>
  </Transfers>
</FlexStatement>"""

    if wrapper == "bare":
        return body.replace("<FlexStatement ", f"<FlexStatement{ns} ", 1)
    if wrapper == "statements":
        return f"<FlexStatements{ns}>{body}</FlexStatements>"
    return (f'<FlexQueryResponse{ns} queryName="TEST QUERY" type="AF">'
            f"<FlexStatements>{body}</FlexStatements></FlexQueryResponse>")


#: 上面夹具的真实叶子计数，供断言使用（不硬编码生产数量）。
EXPECTED_SECTIONS = {
    "account_information": 1,
    "cash_transactions": 2,
    "trades": 2,
    # 4 条余额锚点 + 1 条带流水号的调整 + 入金 + 买入 = 7
    "statement_of_funds": 7,
    "change_in_nav": 2,
    "transfers": 1,
}

#: 4 条余额锚点：USD/HKD 各一对期初期末
EXPECTED_BALANCE_MARKERS = 4
EXPECTED_BALANCE_CURRENCIES = ("HKD", "USD")
#: 带 transactionID 的空 activityCode 行**不是**余额标记，是真实调整
EXPECTED_NON_MARKER_EMPTY_CODE = 1

#: 去重后应得到的唯一经济事件：
#:   入金 1（CashTransaction 与 SOF 同一笔）
#:   利息 1
#:   买入 1（Trade 与 SOF 同一笔）
#:   卖出 1
#:   佣金 2
EXPECTED_DEDUPED_FLOWS = 7
EXPECTED_EXTERNAL_FLOWS = 1


def send_request_ok(reference: str = "REF123456") -> str:
    return (f"<FlexStatementResponse timestamp='01 July, 2026 12:00 PM EDT'>"
            f"<Status>Success</Status><ReferenceCode>{reference}</ReferenceCode>"
            f"<Url>https://example.invalid/GetStatement</Url>"
            f"</FlexStatementResponse>")


def send_request_no_reference() -> str:
    return ("<FlexStatementResponse><Status>Success</Status>"
            "<ReferenceCode></ReferenceCode></FlexStatementResponse>")


def send_request_fail(code: str = "1003", msg: str = "Invalid token") -> str:
    return (f"<FlexStatementResponse><Status>Fail</Status>"
            f"<ErrorCode>{code}</ErrorCode><ErrorMessage>{msg}</ErrorMessage>"
            f"</FlexStatementResponse>")


def statement_not_ready() -> str:
    return ("<FlexStatementResponse><Status>Fail</Status>"
            "<ErrorCode>1019</ErrorCode>"
            "<ErrorMessage>Statement generation in progress</ErrorMessage>"
            "</FlexStatementResponse>")


def empty_response() -> str:
    """结构合法但没有 FlexStatement —— 绝不能被当成成功。"""
    return '<FlexQueryResponse queryName="TEST"><FlexStatements/></FlexQueryResponse>'
