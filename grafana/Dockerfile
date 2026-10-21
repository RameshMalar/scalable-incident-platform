
# Pin the Grafana version for reproducible builds.
FROM grafana/grafana:12.2.0


ENV GF_AZURE_MANAGED_IDENTITY_ENABLED=true


# Provision the Azure Monitor data source and dashboards from Git.
COPY --chown=472:0 provisioning/ /etc/grafana/provisioning/
COPY --chown=472:0 dashboards/ /var/lib/grafana/dashboards/

# Grafana listens on port 3000.
EXPOSE 3000
