variable "aws_region" {
  type    = string
  default = "us-east-1"
}

variable "app_name" {
  type    = string
  default = "hiero-bot"
}

variable "container_image" {
  type    = string
  default = "ghcr.io/anthropicbots/hiero-bot-py:latest"
}

variable "db_password" {
  type        = string
  sensitive   = true
  description = "RDS Postgres master password"
}
