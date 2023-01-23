import requests
import json
import re
import boto3
import time
from datetime import datetime, timezone

s3 = boto3.client('s3')

NR_EVENT_TYPE="NR_Status_Checker" # the event in new relic

def statusMapper(status):
    if(status=="operational"):
        return "✅"
    if(status=="degraded_performance"):
        return "👎"
    if(status=="partial_outage"):
        return "⚠️"
    if(status=="major_outage"):
        return "🔥"
    return status
    

class Checker:
    def __init__(self,event):

        self.current_component_status=None
        self.previous_component_status=None
        self.slack_webhook_urls = event["slack_webhook_urls"] if "slack_webhook_urls" in event else None
        self.s3_bucket=event["s3_bucket"]
        self.s3_filename=event["s3_filename"]
        self.nr_api_key= event["nr_api_key"] if "nr_api_key" in event else None
        self.nr_events_api=event["nr_events_api"] if "nr_events_api" in event else None
        self.nr_metrics_api=event["nr_metrics_api"] if "nr_metrics_api" in event else None


        self.grabPreviousData()         # Get the previous data run from S3 bucket
        self.grab_current_status()      # Get the latest status from the API
        self.detect_changes(event["considered_status"], event["considered_regions"])   # Detect chnages between previous and latest

    def grab_current_status(self):
        print("Grabbing current status...")
        headers = {'Content-Type': 'application/json'}
        url = "https://status.newrelic.com/api/v2/components.json"
        response = requests.get(url, headers=headers)
        self.current_component_status=self.simplify_status(response.json()['components'])
        
        # for testing! Uncomment these to force a state change
        # self.current_component_status["US"]["status"]["degraded_performance"]=2
        # self.current_component_status["US"]["status"]["partial_outage"]=9
        
        self.saveLatestData(self.current_component_status) #save data for next run
    
    def simplify_status(self,status_object):
        status={
            "US": {
                "status": {
                    "total": 0,
                    "operational": 0,
                    "degraded_performance":0,
                    "partial_outage": 0,
                    "major_outage": 0
                },
                "groups": {}
            },
            "EU": {
                "status": {
                    "total": 0,
                    "operational": 0,
                    "degraded_performance":0,
                    "partial_outage": 0,
                    "major_outage": 0
                },
                "groups":{}
            }
        }

        #find all the group parents
        for component in status_object:
            region = "EU" if re.search("^.*\s:\sUS$", component["name"]) == None else "US"
            component_name_match = re.match("^(.+)\s:\s(.+)$",component["name"])
            if component_name_match != None and component["group_id"] == None: #deal with groups
                status[region]["groups"][component["id"]]={
                "group_name": component["name"],
                "group_status": component["status"],
                "components": []
            }

        for component in status_object:
            component_name_match = re.match("^(.+)\s:\s(.+)$",component["name"])
            if component_name_match != None:
                if component["group_id"] != None:
                    region = "EU" if re.search("^.*\s:\sUS$", component["name"]) == None else "US"
                    status[region]["status"]["total"] += 1
                    status[region]["status"][component["status"]] += 1 
                    if(component["status"]!="operational"):
                        status[region]["groups"][component["group_id"]]["components"].append({
                            "name": component_name_match[1],
                            "status":component["status"]
                        })
                        
        return status

    def detect_changes(self,considered_status,considered_regions):
        if self.previous_component_status != None: #first run we might not have a previous so skip
            previous=self.previous_component_status
            latest=self.current_component_status

            for region in considered_regions: # we only care about specified regions
                print("Comparing region",region)
                change_detected = False
                status_changes = {
                    "total": "0",
                    "operational": "0",
                    "degraded_performance":"0",
                    "partial_outage": "0",
                    "major_outage": "0",
                    "groups": ""
                }

                nr_status_changes = {
                    "total": { "current": "0", "previous":"0", "delta":"0"},
                    "operational":  { "current": "0", "previous":"0", "delta":"0"},
                    "degraded_performance": { "current": "0", "previous":"0", "delta":"0"},
                    "partial_outage": { "current": "0", "previous":"0", "delta":"0"},
                    "major_outage":  { "current": "0", "previous":"0", "delta":"0"}
                }
                
                for status in latest[region]["status"]:
                    nr_status_changes[status]["current"]=latest[region]["status"][status]
                    nr_status_changes[status]["previous"]=previous[region]["status"][status]
                    nr_status_changes[status]["delta"]=latest[region]["status"][status] - previous[region]["status"][status]

                    if latest[region]["status"][status] == previous[region]["status"][status]:
                        status_changes[status] = "▶️ " + str(latest[region]["status"][status]) 
                    if latest[region]["status"][status] > previous[region]["status"][status]:
                        status_changes[status] = "⤴️ " + str(latest[region]["status"][status]) + " (was " + str(previous[region]["status"][status]) + ")"
                    if latest[region]["status"][status] < previous[region]["status"][status]:
                        status_changes[status] = "⤵️ " + str(latest[region]["status"][status]) + " (was " + str(previous[region]["status"][status]) + ")"

                    if status in considered_status: #we only care about specified status's to trigger a notification
                        if latest[region]["status"][status] != previous[region]["status"][status]:
                            change_detected = True

                if change_detected == True:
                    print("Changes were detected, sending notifications")
                    status_changes["groups"]=latest[region]["groups"]

                    overallStatus=statusMapper("operational")+ " Operational"
                    if latest[region]["status"]["major_outage"] > 0:
                         overallStatus=statusMapper("major_outage")+ " Major outage"
                    else: 
                        if latest[region]["status"]["partial_outage"] > 0:
                            overallStatus=statusMapper("degraded_performance")+" Partial outage"
                        else: 
                            if latest[region]["status"]["degraded_performance"] > 0:
                                overallStatus=statusMapper("degraded_performance")+" Degraded "

                    self.sendSlackMessage(region,status_changes,overallStatus)
                else:
                    print("No changes were detected")

                self.send_to_nr(nr_status_changes,region)
        

    def sendSlackMessage(self,region,status,overallStatus):
        if self.slack_webhook_urls!=None:
            for slack_webhook_url in self.slack_webhook_urls:
                headers = {'Content-Type': 'application/json'}

                enrichment=""
                for group_key in status["groups"]:
                    if len(status["groups"][group_key]["components"]) > 0:
                        enrichment += "\\n\\n*"+status["groups"][group_key]["group_name"] + "* " + statusMapper(status["groups"][group_key]["group_status"])
                        for component in status["groups"][group_key]["components"]:
                            enrichment += "\\n • "+component["name"] + " " + statusMapper(component["status"])

                template = '''{{
        "blocks": [
            {{
                "type": "section",
                "text": {{
                    "type": "mrkdwn",
                    "text": "*{region}   is   {overallIcon}*   (Status change detected)"
                }}
            }},
            {{
                "type": "divider"
            }},
            {{
                "type": "section",
                "fields": [
                    {{
                        "type": "mrkdwn",
                        "text": "*Status*"
                    }},
                    {{
                        "type": "mrkdwn",
                        "text": "*Components*"
                    }},
                    {{
                        "type": "plain_text",
                        "text": "{operational_icon} Operational",
                        "emoji": true
                    }},
                    {{
                        "type": "plain_text",
                        "text": "{operational}",
                        "emoji": true
                    }},
                    {{
                        "type": "plain_text",
                        "text": "{degraded_performance_icon} Degraded",
                        "emoji": true
                    }},
                    {{
                        "type": "plain_text",
                        "text": "{degraded_performance}",
                        "emoji": true
                    }},
                    {{
                        "type": "plain_text",
                        "text": "{partial_outage_icon} Partial Outage",
                        "emoji": true
                    }},
                    {{
                        "type": "plain_text",
                        "text": "{partial_outage}",
                        "emoji": true
                    }},
                    {{
                        "type": "plain_text",
                        "text": "{major_outage_icon} Major Outage",
                        "emoji": true
                    }},
                    {{
                        "type": "plain_text",
                        "text": "{major_outage}",
                        "emoji": true
                    }}
                ],
                "accessory": {{
                    "type": "button",
                    "text": {{
                        "type": "plain_text",
                        "text": "Status Page",
                        "emoji": true
                    }},
                    "value": "status_page",
                    "url": "https://status.newrelic.com",
                    "action_id": "button-action"
                }}
            }},
            {{
                "type": "divider"
            }},
            {{
                "type": "section",
                "text": {{
                    "type": "plain_text",
                    "text": "Details of affected components:"
                }}
            }},
            {{
                "type": "section",
                "text": {{
                    "type": "mrkdwn",
                    "text": "{enrich}"
                }}
            }}
        ]
    }}'''.format(region="🇺🇸 US" if region=="US" else "🇪🇺 EU", 
                operational=status["operational"],
                degraded_performance=status["degraded_performance"],
                partial_outage=status["partial_outage"],
                major_outage=status["major_outage"],
                enrich="No components affected." if enrichment=="" else str(enrichment),
                operational_icon=statusMapper("operational"),
                degraded_performance_icon=statusMapper("degraded_performance"),
                partial_outage_icon=statusMapper("partial_outage"),
                major_outage_icon=statusMapper("major_outage"),
                overallIcon=overallStatus
                )
                print("Sending slack message...")
                response = requests.post(slack_webhook_url, headers=headers, json=json.loads(template))
                print(response)

    def grabPreviousData(self):
        try:
            response = s3.get_object(Bucket=self.s3_bucket, Key=self.s3_filename)
            data = response['Body'].read().decode('utf-8')
            self.previous_component_status=json.loads(data)
        except Exception as e:
            print(e)
            print('Error getting object from bucket. Make sure they exist and your bucket is in the same region as this function.')
            raise e
    
    def send_to_nr(self,data,region): 
        if self.nr_events_api!=None and self.nr_api_key!=None:
            self.send_to_nr_as_events(data,region)

        if self.nr_metrics_api!=None and self.nr_api_key!=None:
            self.send_to_nr_as_metrics(data,region)

    def send_to_nr_as_metrics(self,data,region): 
        print("Sending Metric data to New Relic...")

        metricsData = []
        timestamp = datetime.now(timezone.utc)
        for status in data:
            for metric in ["current","previous","delta"]:
                metricsData.append(
                    { 
                        "name":"nr_status_check",
                        "type":"gauge",
                        "value":data[status][metric],
                        "timestamp":int(time.mktime(timestamp.timetuple())),
                        "attributes": {
                            "status" : status,
                            "region": region
                        }
                    }
                )
        metrics = [{
            "metrics": metricsData,
            "common": {
                "attributes": {
                    "source":"nr-status-checker"
                }
            }
        }]

        url = self.nr_metrics_api
        headers = {'Content-Type': 'application/json', 'Api-Key': self.nr_api_key}
        try:
            response = requests.post(url, headers=headers, json=metrics)
            print(response)
        except Exception as e:
            print(e)
            print("Error sending data to new relic")

    def send_to_nr_as_events(self,data,region): 
        print("Sending Event data to New Relic...")
        event_data=[]
        for status in data:
            event_data.append(
                {
                    "eventType": NR_EVENT_TYPE,
                    "region": region,
                    "status": status,
                    "current": data[status]["current"],
                    "previous": data[status]["previous"],
                    "delta": data[status]["delta"]
                }
            )
        url = self.nr_events_api
        headers = {'Content-Type': 'application/json', 'Api-Key': self.nr_api_key}
        try:
            response = requests.post(url, headers=headers, json=event_data)
            print(response)
        except Exception as e:
            print(e)
            print("Error sending data to new relic")

        

    def saveLatestData(self,data):
        data_string = json.dumps(data, indent=2, default=str)
        s3.put_object(
            Bucket=self.s3_bucket, 
            Key=self.s3_filename,
            Body=data_string
        )

def lambda_handler(event, context):
 
    checker = Checker(event)

    return "Script complete"
