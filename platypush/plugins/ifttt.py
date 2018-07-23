import requests

from platypush.message import Message
from platypush.plugins import Plugin, action

class IftttPlugin(Plugin):
    """
    This plugin allows you to interact with the IFTTT maker API
    <https://ifttt.com/maker_webhooks> to programmatically trigger your own
    IFTTT hooks from Platypush - e.g. send a tweet or a Facebook post, create a
    Todoist item or a Trello task, trigger events on your mobile device, or run
    any action not natively supported by Platypush but available on your IFTTT
    configuration.

    Requires:

        * **requests** (``pip install requests``)

    Some example usages::

        # Execute a GET request on a JSON endpoint
        {
            "type": "request",
            "action": "http.request.get",
            "args": {
                "url": "http://remote-host/api/v1/entity",
                "params": {
                    "start": "2000-01-01"
                }
            }
        }

        # Execute an action on another Platypush host through HTTP interface
        {
            "type": "request",
            "action": "http.request.post",
            "args": {
                "url": "http://remote-host:8008/execute",
                "json": {
                    "type": "request",
                    "target": "remote-host",
                    "action": "music.mpd.play"
                }
            }
        }
    """

    _base_url = 'https://maker.ifttt.com/trigger/{event_name}/with/key/{ifttt_key}'

    def __init__(self, ifttt_key, *args, **kwargs):
        """
        :param ifttt_key: Your IFTTT Maker API key. Log in to IFTTT and get your key from <https://ifttt.com/maker_webhooks>. Once you've got your key, you can start creating IFTTT rules using the Webhooks channel.
        :type ifttt_key: str
        """

        super().__init__(*args, **kwargs)
        self.ifttt_key = ifttt_key


    @action
    def trigger_event(self, event_name, values=None):
        """
        Send an event to your IFTTT account

        :param event_name: Name of the event
        :type event_name: str

        :param values: Optional list of values to be passed to the event. By convention IFTTT names the values as ``value1,value2,...`.
        :type values: list
        """

        url = self._base_url.format(event_name=event_name, ifttt_key=self.ifttt_key)
        if not values:
            values = []

        response = requests.post(url, json={'value{}'.format(i+1): v
                                            for (i,v) in enumerate(values)})

        if not response.ok:
            raise RuntimeError("IFTTT event '{}' error: {}: {}".format(event_name, r.status_code, r.reason))


# vim:sw=4:ts=4:et:

