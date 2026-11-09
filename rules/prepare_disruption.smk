



rule add_marginal_storage_value:
    input:
        net="path/to/network/file"
    output:
        net="path/to/output/network/file"
    script:
        "scripts/prepare_network_disruptions.py"


