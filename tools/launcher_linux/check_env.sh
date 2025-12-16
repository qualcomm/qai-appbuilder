#!/bin/bash
#
# Library of common environment checks and utility functions.
# Source this script in other scripts to use its functions.
#
# Example usage in another script:
#   source "$(dirname "$0")/check_env.sh"
#   check_qnn_sdk_root

# This function checks if the QNN_SDK_ROOT environment variable is set
# and if the path it points to is a valid, existing directory.
#
# Exits with status 1 if checks fail.
function check_qnn_sdk_root() {
    echo "Checking QNN_SDK_ROOT..."

    # Check if the environment variable is set and not empty
    if [ -z "${QNN_SDK_ROOT}" ]; then
        echo "Error: QNN_SDK_ROOT environment variable is not set." >&2
        echo "Please set it to point to your Qualcomm Neural Network SDK directory." >&2
        return 1
    fi

    # Check if the path exists and is a directory
    if [ ! -d "${QNN_SDK_ROOT}" ]; then
        echo "Error: QNN_SDK_ROOT is set to '${QNN_SDK_ROOT}', but this is not a valid directory." >&2
        return 1
    fi

    echo "Success: QNN_SDK_ROOT is set and valid: ${QNN_SDK_ROOT}"
    return 0
}
