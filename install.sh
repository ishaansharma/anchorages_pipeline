#!/bin/bash

echo "Installing dags"
cp ./port_events_dag.py /dags/port_events_dag.py
cp ./port_visits_dag.py /dags/port_visits_dag.py
echo "Installing post_install.sh"
cp ./post_install.sh /dags/post_install.sh
