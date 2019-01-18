'''
Created on Nov 21, 2015
https://www.lendingclub.com/foliofn/folioInvestingAPIDocument.action

@author: Joey Whelan
'''
import ConfigParser
import logging.handlers
import requests
import json
import decimal
import operator
from contextlib import closing
import csv
import datetime
import time


CONFIG_FILENAME = 'lcInvestor.cfg'
LOG_FILENAME = 'lcInvestor.log'

# Global logger, console + rotating file
logger = logging.getLogger('logapp')
logger.setLevel(logging.INFO)
formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')

fh = logging.handlers.RotatingFileHandler(
    LOG_FILENAME, maxBytes=1000000, backupCount=2)
fh.setLevel(logging.INFO)
fh.setFormatter(formatter)

ch = logging.StreamHandler()
ch.setLevel(logging.INFO)
ch.setFormatter(formatter)
logger.addHandler(fh)
logger.addHandler(ch)


class ConfigData(object):
    """Class for fetching user-configurable options from a file.
    """

    def __init__(self, filename):
        """Fetches user options and sets instance variables.

        Args:
            self: Instance reference
            filename: Name of configuration file

        Returns:
            None

        Raises:
            NoSectionError: Raised if section is missing in config file
            NoOptionError: Raised if option is missing in config file
        """
        logger.debug('Entering ConfigData init(), filename:' + filename)
        cfgParser = ConfigParser.ConfigParser()
        cfgParser.optionxform = str
        cfgParser.read(filename)
        self.investorId = self.castNum(
            cfgParser.get('AccountData', 'investorId'))
        self.ReRunTime = self.castNum(
            cfgParser.get('AccountData', 'ReRunTime')) * 60

        self.authKey = cfgParser.get('AccountData', 'authKey')
        self.buyLoans = cfgParser.get('AccountData', 'buyLoans')
        self.buyTradedNotes = cfgParser.get('AccountData', 'buyTradedNotes')

        self.reserveCash = self.castNum(
            cfgParser.get('AccountData', 'reserveCash'))

        self.investAmount = self.castNum(
            cfgParser.get('AccountData', 'investAmount'))
        # LC rules dictate that investments must be in multiples of $25
        if self.investAmount < 25 or self.investAmount % 25 != 0:
            raise RuntimeError(
                'Invalid investment amount specified in configuration file')
        self.portfolioName = cfgParser.get('AccountData', 'portfolioName')
        criteriaOpts = cfgParser.options(
            'LoanCriteria')  # Loan filtering criteria
        self.criteria = {}
        for opt in criteriaOpts:
            self.criteria[opt] = self.castNum(
                cfgParser.get('LoanCriteria', opt))
            logger.debug('ConfigData init(), opt:' + opt
                         + ' val:' + str(self.criteria[opt]))

        criteriaTradedOpts = cfgParser.options(
            'TradedNotesCriteria')  # Traded Notes filtering criteria
        self.criteriaTraded = {}
        for opt in criteriaTradedOpts:
            self.criteriaTraded[opt] = self.castNum(
                cfgParser.get('TradedNotesCriteria', opt))
            logger.debug('ConfigData init(), opt:' + opt
                         + ' val:' + str(self.criteriaTraded[opt]))
        self.maxNoteAmount = self.castNum(
            cfgParser.get('AccountData', 'maxNoteAmount'))

        logger.debug('Exiting ConfigData init()')

    def castNum(self, val):
        """Determines types of configuration values and casts them accordingly.

        Types are determined via attempts at casting and evaluating any exceptions raised.

        Args:
            self: Instance reference
            val: Value to be tested/casted

        Returns:
            The value casted to the appropriate type (float, Decimal or string)

        Raises:
            None
        """
        logger.debug('Entering castNum, val:' + str(val))
        try:
            i = int(val)
            logger.debug('Exiting castNum, ' + str(val) + ' was an int')
            return i
        except ValueError:
            try:
                d = decimal.Decimal(val)
                logger.debug('Exiting castNum, ' + str(val) + ' was a decimal')
                return d
            except decimal.InvalidOperation:
                logger.debug('Exiting castNum, ' + val + ' was a string')
                return val


class LendingClub(object):
    """Class for accessing the Lending Club REST API.

    Provides simple methods for accessing the LC API.  Additionally, provides logic
    for filtering loans and submitting orders.
    """
    apiVersion = 'v1'

    def __init__(self, config):
        """Initializes state variables for class.

        Args:
            self: Instance reference
            config: Instance of ConfigData

        Returns:
            None

        Raises:
            None
        """
        self.config = config
        self.header = {'Authorization': self.config.authKey,
                       'Content-Type': 'application/json'}
        self.loans = None
        self.cash = None
        self.portfolioId = None
        self.notes = None

        self.acctSummaryURL = 'https://api.lendingclub.com/api/investor/' + LendingClub.apiVersion + \
            '/accounts/' + str(self.config.investorId) + '/summary'
        self.loanListURL = 'https://api.lendingclub.com/api/investor/' + LendingClub.apiVersion + \
            '/loans/listing'
        self.portfoliosURL = 'https://api.lendingclub.com/api/investor/' + LendingClub.apiVersion + \
            '/accounts/' + str(self.config.investorId) + '/portfolios'
        self.ordersURL = 'https://api.lendingclub.com/api/investor/' + LendingClub.apiVersion + \
            '/accounts/' + str(self.config.investorId) + '/orders'
        self.tradesOrdersURL = 'https://api.lendingclub.com/api/investor/' + LendingClub.apiVersion + \
            '/secondarymarket/accounts/' + \
            str(self.config.investorId) + '/orders'

    def __getCash(self):
        """Private method for fetching available cash at Lending Club.

        Args:
            self: Instance reference

        Returns:
            Cash value fetched from Lending Club in decimal format.

        Raises:
            HTTPError:  Any sort of HTTP 400/500 response returned from Lending Club.
        """
        logger.debug('Entering __getCash()')
        resp = requests.get(self.acctSummaryURL, headers=self.header)
        resp.raise_for_status()
        logger.debug('Exiting __getCash()')
        return decimal.Decimal(str(resp.json()['availableCash']))

    def __getLoans(self):
        """Private method for fetching loans and then filtering them based on user criteria.

        Args:
            self: Instance reference

        Returns:
            List of loans that meet the user's criteria.  List is composed of tuples: loan ID, percentage of loan funded.
            List is sorted in reverse order on funding percentage.

        Raises:
            HTTPError:  Any sort of HTTP 400/500 response returned from Lending Club.
        """
        logger.debug('Entering __getLoans()')
        payload = {'showAll': 'true'}
        resp = requests.get(
            self.loanListURL, headers=self.header, params=payload)
        resp.raise_for_status()

        # Compare each available loan to the user's criteria.  Those that match, add the loanID and percentage
        # funded to a dictionary object.  Finally, return a sorted list (of tuples) based on percentage funded.
        loanDict = {}
        for loan in resp.json()['loans']:
            numChecked = 0
            for criterion in self.config.criteria:
                if loan[criterion] == self.config.criteria[criterion]:
                    numChecked += 1
                else:
                    break
            if numChecked == len(self.config.criteria):
                loanDict[loan['id']] = loan['fundedAmount'] / \
                    loan['loanAmount']
                logger.info('Loan id:' + str(loan['id']) +
                            ' was a match, funded percentage = ' + str(loanDict[loan['id']]))
        logger.debug('Exiting __getLoans()')
        return sorted(loanDict.items(), key=operator.itemgetter(1), reverse=True)

    def __getPortfolioId(self):
        """Private method for fetching the portfolio ID for a given portfolio name.

        Uses the config data provided by the user in the configuration file

        Args:
            self: Instance reference

        Returns:
            ID matching the portfolio name

        Raises:
            HTTPError:  Any sort of HTTP 400/500 response returned from Lending Club.
            RuntimeError:  If the user-provided portfolio name does not match any fetched from Lending Club
        """
        logger.debug('Entering __getPortfolioId()')
        resp = requests.get(self.portfoliosURL, headers=self.header)
        resp.raise_for_status()

        for portfolio in resp.json()['myPortfolios']:
            if portfolio['portfolioName'] == self.config.portfolioName:
                portfolioId = portfolio['portfolioId']
                break

        logger.debug('Exiting __getPortfolioId()')
        if portfolioId is None:
            raise RuntimeError(
                'Invalid Portfolio Name specified in configuration file')
        else:
            return portfolioId

    def __postOrder(self, aid, loanId, requestedAmount, portfolioId):
        """Private method for posting a loan order to Lending Club

        Args:
            self: Instance reference
            aid: Account ID, same as Investor ID provided by user in configuration file
            loanId:  ID of loan to be invested in
            requestedAmount: amount to be invested
            portfolioId: Id of portfolio where this loan investment will be placed

        Returns:
            The final amount invested, per Lending Club's API return value.

        Raises:
            HTTPError:  Any sort of HTTP 400/500 response returned from Lending Club.
        """
        logger.debug('Entering __postOrder(), aid:' + str(aid) + ', loanId:' + str(loanId) +
                     ', requestedAmount:' + str(requestedAmount) + ', portfolioId:' + str(portfolioId))
        payload = json.dumps({'aid': aid,
                              'orders': [{'loanId': loanId,
                                          'requestedAmount': float(requestedAmount),
                                          'portfolioId': portfolioId}]})
        resp = requests.post(self.ordersURL, headers=self.header, data=payload)
        retVal = resp.json()

        # Check for the existence of an 'errors' object in the response.
        # If one exists, display the message.  The 'errors' object implies a HTTP error code.
        # That HTTP error will be raised to an exception with raise_for_status()
        if 'errors' in retVal:
            for error in retVal['errors']:
                logger.error('Order error: ' + error['message'])
        resp.raise_for_status()

        # Only 1 order is placed per call of this method.  Pull the first confirmation and log the amount invested.
        confirmation = retVal['orderConfirmations'][0]
        logger.info('OrderId:' + str(retVal['orderInstructId']) + ', $' +
                    str(confirmation['investedAmount']) + ' was invested in loanId:' + str(confirmation['loanId']))
        logger.debug('Exiting __postOrder()')
        return decimal.Decimal(str(confirmation['investedAmount']))

    def hasCash(self):
        """Method for determining if enough funds exist at Lending Club to place an order

        Args:
            self: Instance reference

        Returns:
            Boolean indicating if enough funds exist to execute an order.

        Raises:
            HTTPError:  Any sort of HTTP 400/500 response returned from Lending Club.
        """
        logger.debug('Entering hasCash()')

        self.cash = self.__getCash()
        logger.info('Cash at Lending Club: '
                    + str(self.cash.quantize(decimal.Decimal('.01'), decimal.ROUND_05UP)))
        investMin = self.cash - self.config.reserveCash - self.config.investAmount
        logger.debug('Exiting hasCash()')
        if (investMin >= 0):
            logger.info('Sufficient Cash Available to invest')
            return True
        else:
            logger.info('Insufficient Cash Available to invest')
            return False

    def hasLoans(self):
        """Method for determining if any loans exist that meet the user's requirements

        Args:
            self: Instance reference

        Returns:
            Boolean indicating if matching loans exist

        Raises:
            HTTPError:  Any sort of HTTP 400/500 response returned from Lending Club.
        """
        logger.debug('Entering hasLoans()')
        self.loans = self.__getLoans()

        logger.info('Total number of matching loans available: '
                    + str(len(self.loans)))

        logger.debug('Exiting hasLoans()')
        return len(self.loans) > 0

    def buy(self):
        """Method for submitting a loan order to Lending Club
        Method 'pops' a loan from the matching loan list and deducts the investment amount from the available cash
        balance

        Args:
            self: Instance reference

        Returns:
            None

        Raises:
            HTTPError:  Any sort of HTTP 400/500 response returned from Lending Club.
        """
        logger.debug('Entering buy()')
        if self.portfolioId is None:
            self.portfolioId = self.__getPortfolioId()
        self.cash -= self.__postOrder(self.config.investorId, self.loans.pop(0)[
                                      0], self.config.investAmount, self.portfolioId)
        logger.debug('Exiting buy()')

    def __getTradeNotes(self):
        """method to get the list of notes on trading platform

        """
        notesDict = []
        listingsheader = {'Connection': None, 'Accept-Encoding': None, 'User-Agent': None, 'Authorization': self.config.authKey,
                          'Accept': 'text/csv'}
        with closing(requests.get('https://api.lendingclub.com/api/investor/v1/secondarymarket/listings?updatedSince=' + str(self.config.ReRunTime), stream=True, headers=listingsheader)) as r:
            if r.ok:
                reader = csv.DictReader(
                    r.iter_lines(), delimiter=',', quotechar='"')
                for row in reader:
                    numChecked = 0
                    for criterion in self.config.criteriaTraded:
                        if isinstance(self.config.criteriaTraded[criterion], (int, float, complex)):
                            if float(row[criterion]) <= self.config.criteriaTraded[criterion]:
                                numChecked += 1
                            else:
                                break
                        else:
                            if row[criterion] == str(self.config.criteriaTraded[criterion]):
                                numChecked += 1
                            else:
                                break
                    if numChecked == len(self.config.criteriaTraded):
                        notesDict.append(row)

            else:
                #                print r.request.headers
                logger.info('Notes Server Listing Error: ' + r.reason)

        logger.debug('Exiting __getTradeNotes()')
        return notesDict

    def hasNotes(self):
        """Method for determining if any notes exist that meet the user's requirements

        Args:
            self: Instance reference

        Returns:
            Boolean indicating if matching loans exist

        Raises:
            HTTPError:  Any sort of HTTP 400 / 500 response returned from Lending Club.
        """
        logger.debug('Entering hasLoans()')
        self.notes = self.__getTradeNotes()
        logger.info('Total number of matching Traded Notes available: '
                    + str(len(self.notes)))

        logger.debug('Exiting hasNotes()')
        return len(self.notes) > 0

    def __postNotesOrder(self, aid, noteInfo):
        """Private method for posting a loan order to Lending Club

        Args:
            self: Instance reference
            aid: Account ID, same as Investor ID provided by user in configuration file
            loanId:  ID of loan to be invested in
            requestedAmount: amount to be invested
            portfolioId: Id of portfolio where this loan investment will be placed

        Returns:
            The final amount invested, per Lending Club's API return value.

        Raises:
            HTTPError:  Any sort of HTTP 400 / 500 response returned from Lending Club.
        """
        expirationDate = datetime.datetime.strftime(
            datetime.date.today(), '%Y-%m-%d')

        logger.debug('Entering __postNotesOrder()', 'noteId', str(
            noteInfo['noteId']), 'price', str(noteInfo['price']))
        payload = '[' + json.dumps({'noteId': str(noteInfo['noteId']),
                                    'price': float(noteInfo['price']),
                                    'expirationDate': str(expirationDate),
                                    'orderType': 'BUY'}) + ']'
        resp = requests.post(self.tradesOrdersURL,
                             headers=self.header, data=payload)
        if resp.ok:
            retVal = resp.json()
            resp.raise_for_status()
            print retVal
            # Only 1 order is placed per call of this method.  Pull the first confirmation and log the amount invested.
            try:
                logger.info(str(retVal['status']) + ' $' +
                            float(noteInfo['price']) + ' was invested.')
                logger.debug('Exiting __postNotesOrder()')
                return decimal.Decimal(str(noteInfo['price']))
            except:
                return 0
        else:
            print resp.text
            logger.info('Error __postNotesOrder() ' + resp.reason)
            return 0

    def buyNotes(self):
        """Method for submitting a loan order to Lending Club
        Method 'pops' a loan from the matching loan list and deducts the investment amount from the available cash
        balance

        Args:
            self: Instance reference

        Returns:
            None

        Raises:
            HTTPError:  Any sort of HTTP 400/500 response returned from Lending Club.
        """
        logger.debug('Entering buyNotes()')
        for noteInfo in self.notes:
            if lc.hasCash():
                logger.info('Have Cash buying notes. ')
                if float(noteInfo['price']) < float(self.config.maxNoteAmount):
                    self.cash -= self.__postNotesOrder(
                        self.config.investorId, noteInfo)
        logger.debug('Exiting buyNotes()')


# Main code block.  Instantiates the Lending API access, then loops while cash is available to invest
# and loans meeting the user-defined criteria exist.
try:
    lc = LendingClub(ConfigData(CONFIG_FILENAME))

    while lc.hasCash():
        if (int(lc.config.buyLoans) == 1) and lc.hasLoans():
            lc.buy()
        if (int(lc.config.buyTradedNotes) == 1) and lc.hasNotes():
            lc.buyNotes()

        print(" Sleeping  " + str((lc.config.ReRunTime) / 60) + " min....")
        time.sleep(int(lc.config.ReRunTime))
        print("Running again...")


except:
    logger.exception('')
