import com.pixelmed.codec.jpeg.Parse;

import java.awt.Rectangle;
import java.awt.Shape;
import java.io.BufferedInputStream;
import java.io.BufferedOutputStream;
import java.io.ByteArrayInputStream;
import java.io.ByteArrayOutputStream;
import java.io.DataInputStream;
import java.io.DataOutputStream;
import java.util.Arrays;
import java.util.Vector;

public class PixelMedRedactionBridge {
    private static final byte[] MAGIC = new byte[] {'P', 'M', 'J', 'R', '1'};

    public static void main(String[] args) throws Exception {
        DataInputStream in = new DataInputStream(new BufferedInputStream(System.in));
        DataOutputStream out = new DataOutputStream(new BufferedOutputStream(System.out));

        byte[] magic = new byte[MAGIC.length];
        in.readFully(magic);
        if (!Arrays.equals(magic, MAGIC)) {
            throw new IllegalArgumentException("Bad PixelMed redaction bridge magic");
        }

        int frameCount = in.readInt();
        if (frameCount < 0) {
            throw new IllegalArgumentException("Negative frame count");
        }

        byte[][] redactedFrames = new byte[frameCount][];
        for (int frameIndex = 0; frameIndex < frameCount; frameIndex++) {
            int rectangleCount = in.readInt();
            if (rectangleCount < 0) {
                throw new IllegalArgumentException("Negative rectangle count");
            }

            Vector<Shape> redactionShapes = new Vector<Shape>();
            for (int i = 0; i < rectangleCount; i++) {
                int x = in.readInt();
                int y = in.readInt();
                int width = in.readInt();
                int height = in.readInt();
                if (width > 0 && height > 0) {
                    redactionShapes.add(new Rectangle(x, y, width, height));
                }
            }

            int jpegLength = in.readInt();
            if (jpegLength < 0) {
                throw new IllegalArgumentException("Negative JPEG frame length");
            }
            byte[] jpegBytes = new byte[jpegLength];
            in.readFully(jpegBytes);

            ByteArrayOutputStream redacted = new ByteArrayOutputStream(jpegLength);
            Parse.parse(
                new ByteArrayInputStream(jpegBytes),
                redacted,
                redactionShapes
            );
            redactedFrames[frameIndex] = redacted.toByteArray();
        }

        out.write(MAGIC);
        out.writeInt(frameCount);
        for (byte[] frame : redactedFrames) {
            out.writeInt(frame.length);
            out.write(frame);
        }
        out.flush();
    }
}
