
pipeline {
    agent any

    stages {

        stage('Checkout') {
            steps {
                checkout scm
            }
        }

        stage('Environment information') {
            steps {
                sh 'python3 --version || true'
                sh 'java -version'
            }
        }

        stage('SonarQube Analysis') {
            steps {
                script {
                    def scannerHome = tool 'SonarScanner'

                    withSonarQubeEnv('MySonarQube') {
                        sh """
                            ${scannerHome}/bin/sonar-scanner \
                              -Dsonar.projectKey=agentcore-langgraph-booking \
                              -Dsonar.projectName=agentcore-langgraph-booking \
                              -Dsonar.sources=src,streamlit_app.py \
                              -Dsonar.exclusions=**/__pycache__/**,**/.venv/**
                        """
                    }
                }
            }
        }
    }
}
