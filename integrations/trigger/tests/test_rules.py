'''
Unit test definitions for all rules
'''
import pytest
import trigger
from alertaclient.models.alert import Alert
from mock import MagicMock, patch, DEFAULT


def test_rules_dont_exist():
    '''
    Test the rules file is read
    '''
    with patch('mailer.os') as system_os:
        system_os.path.exists.return_value = False
        res = trigger.parse_group_rules('config_file')
        system_os.path.exists.called_once_with('confile_file')
        assert len(res) is 0


def test_rules_parsing():
    '''
    Test the rules file is properly read
    '''
    with patch.multiple(trigger, os=DEFAULT, open=DEFAULT,
                        json=DEFAULT, validate_rules=DEFAULT) as mocks:
        mocks['os'].path.exists.return_value = True
        mocks['os'].walk().__iter__ \
            .return_value = [('/', None,
                              ['cantopen.json', 'invalid.json', 'valid.json'])]
        invalid_file = MagicMock()
        valid_file = MagicMock()
        mocks['open'].side_effect = [IOError, invalid_file, valid_file]
        doc = [{'notify': {'fields': []}}]
        mocks['json'].load.side_effect = [TypeError, doc]
        mocks['validate_rules'].return_value = doc
        res = trigger.parse_group_rules('config_file')

        # Assert that we checked for folder existence
        mocks['os'].path.exists.called_once_with('confile_file')

        # Check that validation was called for valid file
        mocks['validate_rules'].assert_called_once_with(doc)
        assert mocks['validate_rules'].call_count == 1

        # Assert that we tried to open all 3 files
        assert mocks['open'].call_count == 3
        # Assert that we tried to load 2 files only
        assert mocks['json'].load.call_count == 2
        # Assert that we have proper return value
        assert res == doc


TESTDOCS = [
    ('', False),
    ('String', False),
    ({}, False),
    ([], True),
    ([
         {"rule_name": "invalid_no_fields", }
     ], False),
    ([
         {"rule_name": "invalid_empty_fields",
          "alert_fields": [],
          }
     ], False),
    ([
         {"rule_name": "invalid_no_field_on_fields",
          "alert_fields": [{"regex": r"\d{4}"}],
          }
     ], False),
    ([
         {"rule_name": "invalid_fields_not_list",
          "alert_fields": {"regex": r"\d{4}"},
          }
     ], False),
    ([
         {"rule_name": "invalid_no_fields_regex",
          "alert_fields": [{"field": "test"}],
          }
     ], False),
    ([
         {"rule_name": "invalid_no_fields_regex",
          "alert_fields": [{"field": "tags", "regex": "atag"}],
          "exclude": True,
          }
     ], True),
]


@pytest.mark.parametrize('doc, is_valid', TESTDOCS)
def test_rules_validation(doc, is_valid):
    '''
    Test rule validation
    '''
    res = trigger.validate_rules(doc)
    if is_valid:
        assert res is not None and res == doc
    else:
        assert res is None or res == []


RULES_DATA = [
    ({'resource': '1234', 'event': 'down'},
     [{"rule_name": "Test1",
       "alert_fields": [{"field": "resource", "regex": r"(\w.*)?\d{4}"}],
       "mail": {
           "to_addr": ["test@example.com"]
       },
       "telegram": {
           "chatid": ["-123456"]
       }
       }
      ],
     ['test@example.com'],
     ['-123456'])
]


@pytest.mark.parametrize('alert_spec, input_rules, expected_mail_contacts, expected_telegram_contacts',
                         RULES_DATA)
def test_rules_evaluation(alert_spec, input_rules, expected_mail_contacts, expected_telegram_contacts):
    '''
    Test that rules are properly evaluated
    '''
    with patch.dict(trigger.OPTIONS, trigger.DEFAULT_OPTIONS):
        with patch.dict(trigger.MAIL_OPTIONS, trigger.DEFAULT_MAIL_OPTIONS):
            with patch.dict(trigger.TELEGRAM_OPTIONS, trigger.DEFAULT_TELEGRAM_OPTIONS):
                trigger.OPTIONS['group_rules'] = input_rules
                mail_sender = trigger.Trigger()
                with patch.object(mail_sender, '_send_email_message') as _sem:
                    alert = Alert.parse(alert_spec)
                    emailed_contacts, telegram_contacts = mail_sender.diagnose(alert)
                    assert _sem.call_count == 1
                    assert emailed_contacts == expected_mail_contacts
                    assert telegram_contacts == expected_telegram_contacts


def test_rule_matches_list():
    '''
    Test regex matching is working properly
    for a list
    '''
    # Mock options to instantiate trigger
    with patch.dict(trigger.OPTIONS, trigger.DEFAULT_OPTIONS):
        with patch.dict(trigger.MAIL_OPTIONS, trigger.DEFAULT_MAIL_OPTIONS):
            with patch.dict(trigger.TELEGRAM_OPTIONS, trigger.DEFAULT_TELEGRAM_OPTIONS):
                my_trigger = trigger.Trigger()
                with patch.object(trigger, 're') as regex:
                    regex.match.side_effect = [MagicMock(), None]
                    assert my_trigger._rule_matches('regex', ['item1']) is True
                    regex.match.assert_called_with('regex', 'item1')
                    assert my_trigger._rule_matches('regex', ['item2']) is False
                    regex.match.assert_called_with('regex', 'item2')


def test_rule_matches_string():
    '''
    Test regex matching is working properly
    for a string
    '''
    # Mock options to instantiate mailer
    with patch.dict(trigger.OPTIONS, trigger.DEFAULT_OPTIONS):
        with patch.dict(trigger.MAIL_OPTIONS, trigger.DEFAULT_MAIL_OPTIONS):
            with patch.dict(trigger.TELEGRAM_OPTIONS, trigger.DEFAULT_TELEGRAM_OPTIONS):
                my_trigger = trigger.Trigger()
                with patch.object(trigger, 're') as regex:
                    regex.search.side_effect = [MagicMock(), None]
                    assert my_trigger._rule_matches('regex', 'value1') is True
                    regex.search.assert_called_with('regex', 'value1')
                    assert my_trigger._rule_matches('regex', 'value2') is False
                    regex.search.assert_called_with('regex', 'value2')
