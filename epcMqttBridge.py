"""
    A simple epc-mqtt bridge
    Written by Melan 2022 
    
"""

""" 
# Example config.yml
epcs:
  epc4:
    name: epc4
    ip: 10.208.30.154

hass:
  autodiscoveryTopic: homeassistant
  pollInterval: 30

mqtt:
  host: jarvis.vm.nurd.space
  port: 1883

"""

import yaml
import json
import time
import logging
import requests
import coloredlogs
import paho.mqtt.client 

from bs4 import BeautifulSoup

class epcMqttBridge():
    log = logging.getLogger("MAIN")
    config = {}
    lastPoll = 0

    def __init__(self) -> None:
        self.load_config()
        self.setup_requests()
        self.mqtt = paho.mqtt.client.Client()
        
        self.setup_mqtt()
        self.loop()

    def loop(self):
        """ 
            The polling loop, the polling interval can be set relatively high
            because we will transmit the new state the moment it gets changed anyway. 
        """
        while True:
            if time.time() - self.lastPoll >= int(self.config['hass']['pollInterval']):
                self.poll_states()
                self.lastPoll = time.time()

            self.mqtt.loop(timeout=0.1, max_packets=1)

    def setup_mqtt(self):
        """ Setup paho.mqtt and connect, also gets called when we lose connection. """
        self.mqtt.on_connect = self.on_connect
        self.mqtt.on_message = self.on_message
        self.mqtt.on_disconnect = self.on_disconnect
        
        self.log.info("Connecting to MQTT")
        self.mqtt.connect(self.config['mqtt']['host'], int(self.config['mqtt']['port']), 60)

    def setup_requests(self):
        """
            Setup a requests session so that we setup max_retries
        """
        self.requests = requests.Session()
        adapter = requests.adapters.HTTPAdapter(max_retries=10) #TODO put in config
        self.requests.mount("http://", adapter)

    def on_disconnect(self, client, userdata, rc):
        self.log.error("Disconnected from MQTT! Reconnecting.")
        self.setup_mqtt()

    def on_connect(self, client, userdata, flags, rc):
        """ Subscribe and send discovery and states once we connect """
        self.send_discovery()
        self.poll_states()
        self.mqtt.subscribe("epc/#")

    def get_epc_config(self, name):
        for epc in self.config['epcs']:
            if self.config['epcs'][epc]['name'] == name:
                return self.config['epcs'][epc]
        return None

    def on_message(self, client, userdata, msg):
        """ 
            Handle incoming messages from MQTT and make sure the topic is set
        """
        if msg.topic.endswith("/set"):
            if epc := self.get_epc_config(msg.topic.split('/')[1]):
                state = 1 if msg.payload.decode("utf-8") == "ON" else 0
                # Cast the socket id to an int so we don't end up
                # sending random strings to the epc
                self.set_epc_state(epc, int(msg.topic.split('/')[2]), state)                
            else:
                self.log.error(f"Unknown EPC {msg.topic.split('/')[1]}")

    def send_discovery(self):
        """
            Go over every epc defined in our config and send out a discovery for each socket
        """
        for epc in self.config['epcs']:
            self.log.info(f"Sending discovery for {self.config['epcs'][epc]['name']}")

            for state in self.get_powerstates(self.config['epcs'][epc]['ip']):
                topic = (f"{self.config['hass']['autodiscoveryTopic']}/switch"
                        f"/{self.config['epcs'][epc]['name']}/"
                        f"{self.config['epcs'][epc]['name']}_{state['id']}/config")

                payload = {
                "name": f"{self.config['epcs'][epc]['name']}_{state['id']}",
                "state_topic": f"epc/{self.config['epcs'][epc]['name']}/{state['id']}/state",
                "command_topic": f"epc/{self.config['epcs'][epc]['name']}/{state['id']}/set",
                "unique_id": f"{self.config['epcs'][epc]['name']}_{state['id']}",
                "device":
                    {
                        "identifiers": f"{self.config['epcs'][epc]['name']}",
                        "name": f"{self.config['epcs'][epc]['name']}",
                        "sw_version":"epc-mqtt-bridge",
                        "model":"EPC Mqtt Bridge",
                        "manufacturer":"Melan (NurdSpace)"
                    }
                }
                self.mqtt.publish(topic, json.dumps(payload), retain=True)

    def load_config(self):
        with open("config.yml", "r") as fin:
            self.config = yaml.load(fin, yaml.BaseLoader)

    def request(self, url, retries=0):
        try:
            request = self.requests.get(url, timeout=5) #TODO add to config
            if request.status_code == 200:
                return request.content
            
            self.log.error(f"{url} returned {request.status_code}")
            return ""
        except Exception as e:
            
            if retries >= 10:
                self.log.error(f"Giving up on retrying after {retries} attempts for error {e}")
                return ""
            
            self.log.error(f"Got error {e} for {url}! Re-trying.... {retries + 1}/10")
            retries += 1
            self.request(url, retries)
    
    def poll_states(self):
        """ Connect to every epc, get their states and publish these over MQTT """
        for epc in self.config['epcs']:
            for socket in self.get_powerstates(self.config['epcs'][epc]['ip']):
                stateTopic = f"epc/{self.config['epcs'][epc]['name']}/{socket['id']}/state"
                state = "ON" if socket['state'] == 1 else "OFF" # map 0/1 to OFF/ON for HASS
                self.mqtt.publish(stateTopic, state)

    def set_epc_state(self, epc, id, state):
        """
            Set the state of {id} to {state} and then query the EPC
            to get the new state and publish it over MQTT.
        """

        self.request(f"http://{epc['ip']}/SWOV.CGI?s{id}={state}")
        self.log.info(f"Switching socket {id} on {epc['name']} to {state}")

        for state in self.get_powerstates(epc['ip']):
            if state['id'] == int(id):
                self.log.info(f"Socket {id} on {epc['name']} now has state {state['state']}")
                return self.mqtt.publish(f"epc/{epc['name']}/{state['id']}/state",  
                                  "ON" if state['state'] == 1 else "OFF")

    def get_powerstates(self, epc_host):
        """
            Grab the html page from the epc and extract the powerstate
            of each of the sockets from the <meta> tag
        """
        soup = BeautifulSoup(self.request(f"http://{epc_host}"), "html5lib")
        states = []
        
        for powerstate in soup.findAll("meta", {"http-equiv": "powerstate"}):
            states.append({"id": int(powerstate['content'].split(",")[0]), 
                               "state": int(powerstate['content'].split(",")[1])})
        return states

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    coloredlogs.install(level="INFO", fmt="%(asctime)s %(name)s %(levelname)s %(message)s")
    epcMqttBridge()