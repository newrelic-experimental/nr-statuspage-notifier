#!/bin/bash

#Helper script to set event for schedule. This simply lpoads the event.json into the parameter 'event'. 
#You could hard code this in template.yaml too if you preferred

EVENT=`cat event.json | jq tostring | sed -e 's/^"//' -e 's/"$//'`
sam build
sam deploy $1 --parameter-overrides "event='${EVENT}'"
