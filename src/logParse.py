#!/usr/bin/env python3

import fileinput
import re
import os
import sys
import time
from datetime import datetime
import requests
import json
import argparse
import psycopg2


output_file = "/var/logs/test.json"
statPats = r'^(video|audio)(\s+Transmit:)(\s+Receive:)$'
statPat  = re.compile(statPats)
statFields = {'packets', 'errors', 'pkt.report', 'avg. bitrate', 'lost', 'jitter'}
gw_ip = $(/sbin/ifconfig ens3 | awk '/inet/ {print $2}' | head -n 1)

def extract_data(regex, text):
    match =re.search(regex, text)
    #print(match)
    if match:
        return match.group(1)
    return None


#AV for audio video value: Audio=0 and Video=1
#TR for transmit receive value : Receive=1 and Transmit=0
def extract_stats(regex, text, AV, TR):
    matches = re.finditer(regex, text)
    results = []
    if matches:
        for match in matches:
            results.append(match.groups())
        return results[AV][TR]
    return None

def pushHistory(history, output_file):
    data = {}
    stats = {}
    # connexion à la base de données
    conn = psycopg2.connect(
    host=$DB_HOST,
    port=$DB_PORT,
    database=$DB_DATABASE_NAME,
    user=$DB_USER,
    password=$DB_PASSWORD
    )

    # création d'un curseur
    cur = conn.cursor()


    with open(history, 'r') as f:
            contenu = f.read()

            start_call = extract_data(r"start_call:(\w{3}\s\d{2}\s\d{2}:\d{2}:\d{2})", contenu)

            #convert time to timestamp
            current_year = datetime.now().year
            start_call = datetime.strptime(f"{current_year} {start_call}", "%Y %b %d %H:%M:%S")
            #data["url"] = extract_data(r"url:(.*)", text)
            src = extract_data(r"display_name=([^&]+)",contenu)
            room_id = extract_data(r"room_id=([^&\s]+)",contenu)
            #print(room_id)
            if room_id == "0":
                dst = extract_data(r"room:(.+)\?",contenu)
                ivr = 1
            else:
                dst = room_id
                ivr = 0
            # data["room"] = extract_data(r"room:(.*)", text)
            end_call = extract_data(r"end:(.*)", contenu)
            end_call = datetime.strptime(f"{current_year} {end_call}", "%Y %b %d %H:%M:%S")


            audio_transmit_packets = extract_stats(r"stats:packets:\s+(\d+)\s+(\d+)", contenu, 0 , 0)
            audio_transmit_avg_bitrate = extract_stats(r"stats:avg\. bitrate:\s+([\d.]+)\s+([\d.]+)", contenu, 0 , 0)
            audio_transmit_errors = extract_stats(r"stats:errors:\s+([\d.]+)\s+([\d.]+)", contenu, 0 , 0)
            audio_transmit_pkt_report = extract_stats(r"stats:pkt.report:\s+([\d.]+)\s+([\d.]+)", contenu, 0 , 0)
            audio_transmit_lost = extract_stats(r"lost:\s+([\d.]+)\s+([\d.]+)", contenu, 0 , 0)
            audio_transmit_jitter = extract_stats(r"jitter:\s+([\d.]+)\s+([\d.]+)", contenu, 0 , 0)

            audio_receive_packets = extract_stats(r"stats:packets:\s+(\d+)\s+(\d+)", contenu, 0 , 1)
            audio_receive_avg_bitrate = extract_stats(r"stats:avg\. bitrate:\s+([\d.]+)\s+([\d.]+)", contenu, 0 , 1)
            audio_receive_errors = extract_stats(r"stats:errors:\s+([\d.]+)\s+([\d.]+)", contenu, 0 , 1)
            audio_receive_pkt_report = extract_stats(r"stats:pkt.report:\s+([\d.]+)\s+([\d.]+)", contenu, 0 , 1)
            audio_receive_lost = extract_stats(r"lost:\s+([\d.]+)\s+([\d.]+)", contenu, 0 , 1)
            audio_receive_jitter = extract_stats(r"jitter:\s+([\d.]+)\s+([\d.]+)", contenu, 0 , 1)

            video_transmit_packets = extract_stats(r"stats:packets:\s+(\d+)\s+(\d+)", contenu, 1 , 0)
            video_transmit_avg_bitrate = extract_stats(r"stats:avg\. bitrate:\s+([\d.]+)\s+([\d.]+)", contenu, 1 , 0)
            video_transmit_errors = extract_stats(r"stats:errors:\s+([\d.]+)\s+([\d.]+)", contenu, 1 , 0)
            video_transmit_pkt_report = extract_stats(r"stats:pkt.report:\s+([\d.]+)\s+([\d.]+)", contenu, 1 , 0)
            video_transmit_lost = extract_stats(r"lost:\s+([\d.]+)\s+([\d.]+)", contenu, 1 , 0)
            video_transmit_jitter = extract_stats(r"jitter:\s+([\d.]+)\s+([\d.]+)", contenu, 1 , 0)

            video_receive_packets = extract_stats(r"stats:packets:\s+(\d+)\s+(\d+)", contenu, 1 , 1)
            video_receive_avg_bitrate = extract_stats(r"stats:avg\. bitrate:\s+([\d.]+)\s+([\d.]+)", contenu, 1 , 1)
            video_receive_errors = extract_stats(r"stats:errors:\s+([\d.]+)\s+([\d.]+)", contenu, 1 , 1)
            video_receive_pkt_report = extract_stats(r"stats:pkt.report:\s+([\d.]+)\s+([\d.]+)", contenu, 1 , 1)
            video_receive_lost = extract_stats(r"lost:\s+([\d.]+)\s+([\d.]+)", contenu, 1 , 1)
            video_receive_jitter = extract_stats(r"jitter:\s+([\d.]+)\s+([\d.]+)", contenu, 1 , 1)

            sharing_transmit_packets = extract_stats(r"stats:packets:\s+(\d+)\s+(\d+)", contenu, 2 , 0)
            sharing_transmit_avg_bitrate = extract_stats(r"stats:avg\. bitrate:\s+([\d.]+)\s+([\d.]+)", contenu, 2 , 0)
            sharing_transmit_errors = extract_stats(r"stats:errors:\s+([\d.]+)\s+([\d.]+)", contenu, 2 , 0)

            sharing_receive_packets = extract_stats(r"stats:packets:\s+(\d+)\s+(\d+)", contenu, 2 , 1)
            sharing_receive_avg_bitrate = extract_stats(r"stats:avg\. bitrate:\s+([\d.]+)\s+([\d.]+)", contenu, 2 , 1)
            sharing_receive_errors = extract_stats(r"stats:errors:\s+([\d.]+)\s+([\d.]+)", contenu, 2 , 1)

    cur.execute("INSERT INTO gw_stats(gw_ip, start_call, src, dst, ivr, end_call, audio_transmit_packets, audio_transmit_avg_bitrate, audio_transmit_errors, audio_transmit_pkt_report, audio_transmit_lost, audio_transmit_jitter, audio_receive_packets, audio_receive_avg_bitrate, audio_receive_errors, audio_receive_pkt_report, audio_receive_lost, audio_receive_jitter, video_transmit_packets, video_transmit_avg_bitrate, video_transmit_errors, video_transmit_pkt_report, video_transmit_lost, video_transmit_jitter, video_receive_packets, video_receive_avg_bitrate, video_receive_errors, video_receive_pkt_report, video_receive_lost, video_receive_jitter, sharing_transmit_packets, sharing_transmit_avg_bitrate, sharing_transmit_errors, sharing_receive_packets, sharing_receive_avg_bitrate, sharing_receive_errors) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)", (gw_ip, start_call, src, dst, ivr, end_call, audio_transmit_packets, audio_transmit_avg_bitrate, audio_transmit_errors, audio_transmit_pkt_report, audio_transmit_lost, audio_transmit_jitter, audio_receive_packets, audio_receive_avg_bitrate, audio_receive_errors, audio_receive_pkt_report, audio_receive_lost, audio_receive_jitter, video_transmit_packets, video_transmit_avg_bitrate, video_transmit_errors, video_transmit_pkt_report, video_transmit_lost, video_transmit_jitter, video_receive_packets, video_receive_avg_bitrate, video_receive_errors, video_receive_pkt_report, video_receive_lost, video_receive_jitter, sharing_transmit_packets, sharing_transmit_avg_bitrate, sharing_transmit_errors, sharing_receive_packets, sharing_receive_avg_bitrate, sharing_receive_errors))
    #"INSERT INTO gw_stats (json_data) VALUES (%s)", (json_str,))
    # validation de la transaction
    conn.commit()

    # fermeture du curseur et de la connexion
    cur.close()
    conn.close()


def getLogsData (log, key, history):
    try:
        f = open(history, 'a')
        f.write('{}:{}'.format(key, log))
        f.close()
        if key == 'end' and output_file:
            pushHistory(history,output_file)
    except Exception as exc:
        print("Failed to get logs data: ", exc)


def main():
    inputs = sys.argv
    parser = argparse.ArgumentParser(description='Log parser')
    parser.add_argument('-p','--pref', help='log prefix', required=True)
    parser.add_argument('-i','--history', help='history file', required=False)
    inputs = vars(parser.parse_args())
    pref = inputs['pref']
    with open('/dev/stdin') as f:
        for l in f:
            line = l.rstrip()
            ansiEscape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
            line = ansiEscape.sub('', line)
            if not line:
                continue
            print('{}: {}'.format(pref, line), flush=True)

            if not inputs['history']:
                continue
            historyFile = inputs['history']


            try:
                if 'Web browsing URL:' in line:
                    getLogsData (datetime.now().strftime("%b %d %H:%M:%S"),
                                 'start_call', historyFile)
                    getLogsData ('{}\n'.format(line.split('URL: ',1)[1]),
                                 'url', historyFile)



                if 'Jitsi URL:' in line:
                    getLogsData ('{}\n'.format(line.split('#',1)[0].
                                               rsplit("/",1)[1]),
                                 'room', historyFile)
                    #print("hello7")

                if pref == "Baresip":
                    if (statPat.fullmatch(line) or line.split(':')[0] in statFields):
                        getLogsData ('{}\n'.format(line),
                                     'stats', historyFile)
                    #print("hello8")

                if 'Browsing stopped' in line:
                    getLogsData (datetime.now().strftime("%b %d %H:%M:%S"),
                                 'end', historyFile)
                    #print("hello9")

            except Exception as exc:
                print("Failed to parse log line: ", exc)

if __name__ == "__main__":
    main()

