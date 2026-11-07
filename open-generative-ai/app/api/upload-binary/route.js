import { NextResponse } from 'next/server';
import { validateUploadProxyTarget, getApiKeyFromRequest, isBlockedFileType } from '../../../src/lib/uploadProxyTarget';

export async function POST(request) {
    try {
        const apiKey = getApiKeyFromRequest(request);
        if (!apiKey) {
            return NextResponse.json({ error: 'Unauthorized: Missing API key' }, { status: 401 });
        }

        const formData = await request.formData();

        // Extract the original S3 target URL we injected earlier
        const targetUrl = formData.get('x-proxy-target-url');

        if (!targetUrl) {
            return NextResponse.json({ error: 'Missing proxy target URL' }, { status: 400 });
        }

        const validatedTarget = validateUploadProxyTarget(targetUrl);
        if (!validatedTarget.ok) {
            return NextResponse.json(
                { error: 'Invalid upload target', reason: validatedTarget.reason },
                { status: 400 }
            );
        }

        // Validate file content type and extension to prevent dangerous uploads (e.g. HTML, SVG, executables)
        const fileContentType = formData.get('Content-Type') || formData.get('content-type') || '';
        const keyName = formData.get('key') || '';
        
        for (const [key, value] of formData.entries()) {
            if (value && typeof value === 'object' && typeof value.name === 'string') {
                if (isBlockedFileType(value.name, value.type || fileContentType)) {
                    return NextResponse.json(
                        { error: 'Invalid file type', reason: 'blocked_file_type' },
                        { status: 400 }
                    );
                }
            }
        }

        if (isBlockedFileType(keyName, fileContentType)) {
            return NextResponse.json(
                { error: 'Invalid file type', reason: 'blocked_file_type' },
                { status: 400 }
            );
        }

        // Reconstruct the FormData for S3 (excluding our internal proxy marker)
        const s3FormData = new FormData();
        
        // S3 is very sensitive to field ordering. We must ensure 'file' is likely last
        // or at least that all signature fields come before what S3 expects.
        for (const [key, value] of formData.entries()) {
            if (key !== 'x-proxy-target-url') {
                s3FormData.append(key, value);
            }
        }

        // Perform the server-to-server POST to S3
        const s3Response = await fetch(validatedTarget.url, {
            method: 'POST',
            body: s3FormData,
        });

        if (s3Response.ok || s3Response.status === 204) {
            return new Response(null, { status: 204 });
        } else {
            const errorText = await s3Response.text();
            console.error('S3 Proxy Error:', errorText);
            return new Response(errorText, { status: s3Response.status });
        }
    } catch (error) {
        console.error('Upload Proxy Exception:', error);
        return NextResponse.json({ error: error.message }, { status: 500 });
    }
}

