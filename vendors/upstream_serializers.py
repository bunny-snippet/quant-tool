from rest_framework import serializers


class UpstreamCredentialMetadataSerializer(serializers.Serializer):
    source = serializers.CharField()
    environment_variables = serializers.ListField(child=serializers.CharField())
    configured = serializers.BooleanField()
    authentication = serializers.CharField()


class UpstreamOperationMetadataSerializer(serializers.Serializer):
    code = serializers.CharField()
    label = serializers.CharField()
    description = serializers.CharField(required=False)
    upstream_method = serializers.CharField()
    api_url = serializers.CharField()
    documentation_url = serializers.URLField(allow_blank=True)
    required_parameters = serializers.ListField(child=serializers.CharField(), required=False)
    query_parameters = serializers.ListField(child=serializers.CharField(), required=False)
    body_parameters = serializers.ListField(child=serializers.CharField(), required=False)
    mutating = serializers.BooleanField(default=False)
    response_description = serializers.CharField(required=False)


class UpstreamIntegrationMetadataSerializer(serializers.Serializer):
    client_code = serializers.SlugField()
    lookup_aliases = serializers.ListField(child=serializers.CharField())
    client = serializers.CharField()
    integration = serializers.CharField()
    provider = serializers.CharField()
    base_url = serializers.URLField()
    active = serializers.BooleanField()
    credential = UpstreamCredentialMetadataSerializer()
    operations = UpstreamOperationMetadataSerializer(many=True)


class UpstreamExecutionIntegrationSerializer(serializers.Serializer):
    client_code = serializers.SlugField()
    client = serializers.CharField()
    name = serializers.CharField()
    provider = serializers.CharField()


class UpstreamExecutionResponseSerializer(serializers.Serializer):
    integration = UpstreamExecutionIntegrationSerializer()
    operation = UpstreamOperationMetadataSerializer()
    credential = UpstreamCredentialMetadataSerializer()
    result = serializers.JSONField()
    total_rows_in_response = serializers.IntegerField(allow_null=True)
    response_truncated = serializers.BooleanField()
    response_limit = serializers.IntegerField()


class UpstreamErrorSerializer(serializers.Serializer):
    detail = serializers.CharField()


class UpstreamMutationRequestSerializer(serializers.Serializer):
    confirm_upstream_mutation = serializers.BooleanField(
        help_text="Must be true. The request changes live data in the provider account."
    )
    survey_id = serializers.CharField(required=False, help_text="InnovateMR Survey ID, when required.")
    pid = serializers.CharField(required=False, help_text="Supplier panelist PID, when required.")
    payload = serializers.JSONField(
        required=False,
        help_text="Exact documented InnovateMR JSON body. Credentials are added server-side.",
    )


class RFGCallbackPreviewSerializer(serializers.Serializer):
    result = serializers.CharField(help_text="RFG result code, for example 1, 2, 7, 30 or 50.")
    ruledOutBy = serializers.CharField(required=False, allow_blank=True)
    liveP = serializers.CharField(required=False, allow_blank=True)
    liveS = serializers.CharField(required=False, allow_blank=True)
    liveI = serializers.CharField(required=False, allow_blank=True)
    quotaThrottle = serializers.CharField(required=False, allow_blank=True)
