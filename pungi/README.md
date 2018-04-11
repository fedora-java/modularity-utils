Generating compose
------------------

1. Log into staging composer via SSH.  If you don't have access, join `modularity-wg` group in FAS.

    composer.stg.phx2.fedoraproject.org

2. Clone this git repository.

    git clone https://github.com/fedora-java/modularity-utils.git
    cd ./modularity-utils/pungi/

3. Install Koji client config.

    mkdir -p ~/.koji
    ln -sf $PWD/koji.conf ~/.koji/config

4. Run compose script.

    ./compose.sh
